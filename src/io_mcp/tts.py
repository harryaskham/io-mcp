"""TTS engine with two backends: local espeak-ng and API tts tool.

Supports pregeneration: generate audio files for a batch of texts in
parallel, then play them instantly on demand from cache.

The tts tool is configured via IoMcpConfig (config.yml) which passes
explicit CLI flags for provider, model, voice, speed, base-url, api-key.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tempfile
import threading
import time as _time_mod
from typing import TYPE_CHECKING, Optional

from .subprocess_manager import AsyncSubprocessManager
from .logging import get_logger, log_context, TUI_ERROR_LOG

if TYPE_CHECKING:
    from .config import IoMcpConfig

_log = get_logger("io-mcp.tts", TUI_ERROR_LOG)


# espeak-ng words per minute
TTS_SPEED = 160

# Path to the gpt-4o-mini-tts wrapper

# LD_LIBRARY_PATH needed for sounddevice/portaudio on NOD
PORTAUDIO_LIB = os.path.expanduser("~/.nix-profile/lib")

# Cache dir for pregenerated audio
CACHE_DIR = os.path.join(tempfile.gettempdir(), "io-mcp-tts-cache")


def _find_binary(name: str) -> Optional[str]:
    """Find a binary in PATH or common Nix locations."""
    found = shutil.which(name)
    if found:
        return found
    for path in [
        f"/data/data/com.termux.nix/files/home/.nix-profile/bin/{name}",
        f"/nix/var/nix/profiles/default/bin/{name}",
    ]:
        if os.path.isfile(path):
            return path
    return None


class TTSEngine:
    """Text-to-speech with three backends and pregeneration support.

    - termux: termux-tts-speak via Android TTS (nice voice, instant, no PulseAudio)
    - local:  espeak-ng (fast, robotic, file-based)
    - api:    tts CLI tool (best voice, slower) — configured via IoMcpConfig

    API audio is generated to WAV files, then played via paplay.
    pregenerate() creates clips in parallel so scrolling is instant.
    speak_streaming() pipes tts stdout → paplay for faster first-audio.
    termux-tts-speak outputs directly to Android media stream (no files).
    """

    def __init__(self, local: bool = False, speed: float = 1.0,
                 config: Optional["IoMcpConfig"] = None):
        # Centralised subprocess manager — replaces manual process tracking
        # (self._process, self._streaming_tts_proc, self._termux_proc) and
        # threading.Lock. Tags: "playback", "tts_stream", "termux"
        self._mgr = AsyncSubprocessManager()
        self._local = local
        self._speed = speed
        self._muted = False  # when True, play_cached is a no-op
        self._config = config

        # Sequential speech lock — ensures only one speech plays at a time.
        # speak() and speak_async() acquire this to serialize playback.
        # speak_with_local_fallback() (scroll/select overlay) does NOT
        # acquire this — it interrupts via stop_sync() instead.
        # Uses RLock so _generate_to_file can re-acquire from within speak().
        self._speech_lock = threading.RLock()

        # API concurrency lock — ensures only one tts CLI process runs
        # at a time. Acquired by both speech (speak/speak_streaming) and
        # pregeneration (_generate_to_file) to prevent API rate limiting.
        self._api_lock = threading.Lock()

        # TTS error callback — set by the TUI to show errors visually
        self._on_tts_error = None

        # PulseAudio/paplay health tracking
        self._consecutive_failures = 0
        self._last_failure_time: float = 0
        self._last_failure_msg: str = ""
        self._total_failures = 0
        self._total_plays = 0
        self._max_retries = 2  # retry paplay this many times on failure

        # API TTS generation failure tracking — when the API key is missing
        # or the service is down, avoid spawning 30s-timeout processes on
        # every scroll event.  After _API_FAIL_THRESHOLD consecutive failures,
        # skip background API generation entirely.
        self._api_gen_consecutive_failures = 0
        self._api_gen_last_failure: float = 0
        _API_FAIL_THRESHOLD = 3
        _API_FAIL_COOLDOWN = 60  # retry API after 60s
        self._api_fail_threshold = _API_FAIL_THRESHOLD
        self._api_fail_cooldown = _API_FAIL_COOLDOWN

        self._env = os.environ.copy()
        self._env["PULSE_SERVER"] = os.environ.get("PULSE_SERVER", "127.0.0.1")
        # Prepend the portaudio lib path, preserving any existing LD_LIBRARY_PATH
        # (e.g. Nix-provided paths that include the correct portaudio store path).
        # CRITICAL: Remove empty path segments (::) from LD_LIBRARY_PATH.
        # On Nix-on-Droid, multiple wrapper scripts layer their paths using
        # bash string manipulation that can create empty segments (:::).
        # Python's ctypes.util.find_library breaks when LD_LIBRARY_PATH
        # contains empty segments — it returns None even when the library
        # exists in one of the valid paths. This causes sounddevice to fail
        # with "PortAudio library not found" in the tts CLI.
        existing_ldpath = os.environ.get("LD_LIBRARY_PATH", "")
        if existing_ldpath:
            combined = f"{PORTAUDIO_LIB}:{existing_ldpath}"
        else:
            combined = PORTAUDIO_LIB
        # Clean up empty segments that break ctypes.util.find_library
        self._env["LD_LIBRARY_PATH"] = ":".join(
            p for p in combined.split(":") if p
        )

        self._paplay = _find_binary("paplay")
        self._espeak = _find_binary("espeak-ng")
        self._tts_bin = _find_binary("tts")
        self._termux_exec = _find_binary("termux-exec")

        # Local TTS backend preference (for scroll readout fallback)
        local_backend = config.tts_local_backend if config else "termux"
        if local_backend == "termux" and not self._termux_exec:
            local_backend = "espeak"  # fall back if termux-exec not available
        if local_backend == "espeak" and not self._espeak:
            local_backend = "none"
        self._local_backend = local_backend

        if not self._paplay:
            print("WARNING: paplay not found — TTS disabled", flush=True)

        if self._local and not self._espeak:
            print("WARNING: espeak-ng not found — TTS disabled", flush=True)

        if not self._local and not self._tts_bin:
            print("WARNING: tts tool not found in PATH — falling back to espeak-ng", flush=True)
            self._local = True

        # Audio cache: text hash → file path
        self._cache: dict[str, str] = {}
        os.makedirs(CACHE_DIR, exist_ok=True)

        # Scroll generation counter — incremented on each speak_with_local_fallback
        # call. Background threads check this before playing to avoid stale audio
        # overlapping with newer requests.
        self._scroll_gen = 0

        # Pregeneration generation counter — incremented on each pregenerate()
        # call. Workers check this before starting a new API call and skip
        # if a newer generation has been requested (stale choices).
        self._pregen_gen = 0

        # Separate UI pregeneration counter — UI texts (settings, extra
        # options) are pregenerated in their own queue so they don't
        # compete with agent choice pregeneration for API bandwidth.
        self._pregen_ui_gen = 0

        mode = "espeak-ng (local)" if self._local else "tts CLI (API)"
        if not self._local and self._config:
            mode = f"{self._config.tts_voice_preset} ({self._config.tts_model_name})"
        local_mode = {"termux": "termux-tts-speak", "espeak": "espeak-ng", "none": "none"}[self._local_backend]
        print(f"  TTS engine: {mode}", flush=True)
        print(f"  TTS local: {local_mode}", flush=True)

    def _cache_key(self, text: str, voice_override: Optional[str] = None,
                   emotion_override: Optional[str] = None,
                   model_override: Optional[str] = None,
                   speed_override: Optional[float] = None) -> str:
        # Include backend, speed, and config-based settings in cache key
        # so cache is invalidated when voice/model/speed changes
        if self._config and not self._local:
            voice = voice_override or self._config.tts_voice
            emotion = emotion_override or self._config.tts_emotion
            model = model_override or self._config.tts_model_name
            speed = speed_override if speed_override is not None else self._config.tts_speed
            params = (
                f"{text}|local={self._local}"
                f"|model={model}"
                f"|voice={voice}"
                f"|speed={speed}"
                f"|emotion={emotion}"
            )
        else:
            params = f"{text}|local={self._local}|speed={self._speed}"
        return hashlib.md5(params.encode()).hexdigest()

    # ─── Failure tracking and health ──────────────────────────────

    def _record_failure(self, message: str) -> None:
        """Record a paplay/TTS failure for health tracking and logging."""
        self._consecutive_failures += 1
        self._total_failures += 1
        self._last_failure_time = _time_mod.time()
        self._last_failure_msg = message
        _log.warning(
            "TTS playback failure: %s", message,
            extra={"context": log_context(
                consecutive=self._consecutive_failures,
                total=self._total_failures,
                pulse_server=self._env.get("PULSE_SERVER", "unset"),
            )},
        )

    def _log_recovery(self, attempt: int) -> None:
        """Log recovery from playback failures."""
        _log.info(
            "TTS recovered after %d failure(s), retry attempt %d",
            self._consecutive_failures, attempt,
        )

    def _log_tts_error(self, message: str, text: str = "") -> None:
        """Log a TTS generation error for diagnostics."""
        preview = text[:80] + ("..." if len(text) > 80 else "")
        _log.error(
            "TTS generation failure: %s", message,
            extra={"context": log_context(
                text_preview=preview,
                pulse_server=self._env.get("PULSE_SERVER", "unset"),
                tts_bin=self._tts_bin,
            )},
        )

    def _api_gen_available(self) -> bool:
        """Check whether API TTS generation should be attempted.

        Returns False when we've seen too many consecutive API failures
        (e.g. missing API key) to avoid spawning 30s-timeout processes
        that pile up and make the TUI sluggish.  Resets after a cooldown.
        """
        if self._api_gen_consecutive_failures < self._api_fail_threshold:
            return True
        # Check cooldown — maybe the key was restored
        if _time_mod.time() - self._api_gen_last_failure > self._api_fail_cooldown:
            self._api_gen_consecutive_failures = 0
            return True
        return False

    def _record_api_gen_failure(self) -> None:
        """Record an API TTS generation failure."""
        self._api_gen_consecutive_failures += 1
        self._api_gen_last_failure = _time_mod.time()

    def _record_api_gen_success(self) -> None:
        """Record a successful API TTS generation."""
        self._api_gen_consecutive_failures = 0

    def reset_failure_counters(self) -> None:
        """Reset all failure counters (API gen + playback).

        Called by the TUI when PulseAudio recovers — PulseAudio outages
        cause paplay failures that get misattributed as API failures,
        so clearing counters lets TTS resume immediately.
        """
        self._api_gen_consecutive_failures = 0
        self._consecutive_failures = 0

    @property
    def tts_health(self) -> dict:
        """Return TTS health status for diagnostics.

        Returns a dict with:
            status: "ok", "degraded" (recent failures but recovered), "failing" (consecutive failures)
            consecutive_failures: number of failures in a row
            total_failures: total lifetime failures
            total_plays: total successful plays
            last_failure: last failure message (if any)
            last_failure_ago: seconds since last failure (if any)
        """
        now = _time_mod.time()
        result = {
            "consecutive_failures": self._consecutive_failures,
            "total_failures": self._total_failures,
            "total_plays": self._total_plays,
            "last_failure": self._last_failure_msg or None,
            "last_failure_ago": round(now - self._last_failure_time, 1) if self._last_failure_time else None,
        }
        if self._consecutive_failures >= 3:
            result["status"] = "failing"
        elif self._total_failures > 0 and (now - self._last_failure_time) < 300:
            result["status"] = "degraded"
        else:
            result["status"] = "ok"
        return result

    def _generate_to_file(self, text: str, voice_override: Optional[str] = None,
                          emotion_override: Optional[str] = None,
                          model_override: Optional[str] = None,
                          speed_override: Optional[float] = None) -> Optional[str]:
        """Generate audio for text and save to a WAV file. Returns file path.

        Acquires both the speech lock (to wait for active speech to finish)
        and the API lock (to prevent concurrent API calls from other
        pregeneration threads). This ensures only one tts CLI process
        runs at a time across all speech and generation.

        Uses RLock for speech_lock so speak() → play_cached() → _generate_to_file()
        doesn't deadlock (the same thread re-acquires the lock).
        """
        key = self._cache_key(text, voice_override, emotion_override,
                              model_override=model_override,
                              speed_override=speed_override)

        # Check cache (no lock needed)
        cached = self._cache.get(key)
        if cached and os.path.isfile(cached):
            return cached

        out_path = os.path.join(CACHE_DIR, f"{key}.wav")

        # Wait for any active speech to finish, then hold the API lock
        with self._speech_lock:
            with self._api_lock:
                # Re-check cache after acquiring locks
                cached = self._cache.get(key)
                if cached and os.path.isfile(cached):
                    return cached

                try:
                    if self._local:
                        if not self._espeak:
                            self._log_tts_error("espeak-ng not available", text)
                            return None
                        wpm = int(TTS_SPEED * self._speed)
                        cmd = [self._espeak, "--stdout", "-s", str(wpm), text]
                        with open(out_path, "wb") as f:
                            proc = subprocess.run(
                                cmd, stdout=f, stderr=subprocess.PIPE,
                                env=self._env, timeout=10,
                            )
                        if proc.returncode != 0:
                            stderr_out = (proc.stderr or b"").decode("utf-8", errors="replace").strip()
                            self._log_tts_error(
                                f"espeak-ng failed (code {proc.returncode}): {stderr_out}", text)
                            return None
                    else:
                        if not self._tts_bin:
                            self._log_tts_error("tts binary not available", text)
                            self._record_api_gen_failure()
                            return None

                        if not self._api_gen_available():
                            return None

                        if self._config:
                            cmd = [self._tts_bin] + self._config.tts_cli_args(
                                text, voice_override=voice_override,
                                emotion_override=emotion_override,
                                model_override=model_override,
                                speed_override=speed_override)
                        else:
                            cmd = [self._tts_bin, text, "--stdout", "--response-format", "wav"]

                        with open(out_path, "wb") as f:
                            proc = subprocess.run(
                                cmd, stdout=f, stderr=subprocess.PIPE,
                                env=self._env, timeout=15,
                            )
                        if proc.returncode != 0:
                            stderr_out = (proc.stderr or b"").decode("utf-8", errors="replace").strip()
                            self._log_tts_error(
                                f"tts CLI failed (code {proc.returncode}): {stderr_out}", text)
                            self._record_api_gen_failure()
                            try:
                                os.unlink(out_path)
                            except OSError:
                                pass
                            return None

                        try:
                            fsize = os.path.getsize(out_path)
                            if fsize < 44:
                                self._log_tts_error(
                                    f"tts CLI produced invalid WAV ({fsize} bytes)", text)
                                self._record_api_gen_failure()
                                try:
                                    os.unlink(out_path)
                                except OSError:
                                    pass
                                return None
                        except OSError:
                            pass

                    self._cache[key] = out_path
                    self._record_api_gen_success()
                    return out_path

                except subprocess.TimeoutExpired:
                    self._log_tts_error("TTS generation timed out", text)
                    if not self._local:
                        self._record_api_gen_failure()
                    return None
                except Exception as e:
                    self._log_tts_error(f"TTS generation exception: {e}", text)
                    if not self._local:
                        self._record_api_gen_failure()
                    return None

    # ─── Fragment-based TTS ─────────────────────────────────────

    # Number word lookup for fragment-based scroll readout
    _NUMBER_WORDS = {
        1: "one", 2: "two", 3: "three", 4: "four", 5: "five",
        6: "six", 7: "seven", 8: "eight", 9: "nine",
    }

    def _concat_wavs(self, paths: list[str]) -> Optional[str]:
        """Concatenate multiple WAV files into one. Returns path to combined file.

        All WAVs from the tts CLI are 24kHz mono 16-bit PCM, so we can
        simply concatenate the raw PCM data and write a new header.
        Skips files that don't exist or are too small (< 44 bytes).
        """
        import struct as _struct

        pcm_chunks: list[bytes] = []
        sample_rate = 24000
        bits_per_sample = 16
        channels = 1

        for p in paths:
            try:
                with open(p, "rb") as f:
                    header = f.read(44)
                    if len(header) < 44:
                        continue
                    # Validate RIFF/WAVE header
                    if header[:4] != b"RIFF" or header[8:12] != b"WAVE":
                        continue
                    # Read format from this file (use first file's params)
                    fmt_audio, fmt_channels, fmt_rate = _struct.unpack("<HHI", header[20:28])
                    _, _, fmt_bits = _struct.unpack("<IHH", header[28:36])
                    if len(pcm_chunks) == 0:
                        sample_rate = fmt_rate
                        channels = fmt_channels
                        bits_per_sample = fmt_bits
                    pcm_data = f.read()
                    if pcm_data:
                        pcm_chunks.append(pcm_data)
            except (OSError, _struct.error):
                continue

        if not pcm_chunks:
            return None

        # Build combined WAV
        total_pcm = b"".join(pcm_chunks)
        byte_rate = sample_rate * channels * (bits_per_sample // 8)
        block_align = channels * (bits_per_sample // 8)

        # WAV header (44 bytes)
        header = _struct.pack(
            "<4sI4s4sIHHIIHH4sI",
            b"RIFF",
            36 + len(total_pcm),
            b"WAVE",
            b"fmt ",
            16,  # PCM fmt chunk size
            1,   # PCM format
            channels,
            sample_rate,
            byte_rate,
            block_align,
            bits_per_sample,
            b"data",
            len(total_pcm),
        )

        combined_key = hashlib.md5(b"".join(
            p.encode() for p in paths
        )).hexdigest()
        out_path = os.path.join(CACHE_DIR, f"concat_{combined_key}.wav")
        try:
            with open(out_path, "wb") as f:
                f.write(header)
                f.write(total_pcm)
        except OSError:
            return None
        return out_path

    def speak_fragments(self, fragments: list[str],
                        voice_override: Optional[str] = None,
                        emotion_override: Optional[str] = None,
                        speed_override: Optional[float] = None) -> None:
        """Play a sequence of text fragments as concatenated audio.

        Each fragment is generated/cached individually, then all WAVs
        are concatenated into a single file for gapless playback.
        Falls back to speak_async() of the full text if any fragment
        is missing or concatenation fails.

        Designed for selection confirmation: fragments like ["selected",
        "Fix a bug"] are cached individually. The word "selected" is
        reused across all choices, drastically reducing API calls.

        Runs asynchronously in a background thread, queuing behind
        current speech via the speech lock.
        """
        def _do():
            try:
                with self._speech_lock:
                    if self._muted or not self._paplay:
                        return

                    # Collect cached paths for each fragment
                    paths: list[str] = []
                    for frag in fragments:
                        key = self._cache_key(frag, voice_override, emotion_override,
                                              speed_override=speed_override)
                        path = self._cache.get(key)
                        if path and os.path.isfile(path):
                            paths.append(path)
                        else:
                            # Fragment not cached — fall back to full text
                            full_text = " ".join(fragments)
                            # Use streaming for uncached (generates + plays)
                            if not self._local and self._tts_bin and self._config:
                                self.speak_streaming(full_text,
                                                     voice_override=voice_override,
                                                     emotion_override=emotion_override,
                                                     speed_override=speed_override,
                                                     block=True)
                            else:
                                self.play_cached(full_text, block=True,
                                                 voice_override=voice_override,
                                                 emotion_override=emotion_override,
                                                 speed_override=speed_override)
                            return

                    # All fragments cached — concatenate and play
                    combined = self._concat_wavs(paths)
                    if combined:
                        _time_mod.sleep(0.05)  # Brief pause for PulseAudio
                        self._start_playback(combined, max_attempts=self._max_retries)
                        self._wait_for_playback()
                    else:
                        # Concatenation failed — fall back
                        full_text = " ".join(fragments)
                        if not self._local and self._tts_bin and self._config:
                            self.speak_streaming(full_text,
                                                 voice_override=voice_override,
                                                 emotion_override=emotion_override,
                                                 speed_override=speed_override,
                                                 block=True)
                        else:
                            self.play_cached(full_text, block=True,
                                             voice_override=voice_override,
                                             emotion_override=emotion_override,
                                             speed_override=speed_override)
            except Exception as e:
                _log.error("speak_fragments error", exc_info=True)
        threading.Thread(target=_do, daemon=True).start()

    def speak_fragments_scroll(self, fragments: list[str],
                               voice_override: Optional[str] = None,
                               emotion_override: Optional[str] = None,
                               speed_override: Optional[float] = None) -> None:
        """Scroll-aware fragment playback. Stops current audio first.

        Like speak_with_local_fallback but uses fragment concatenation
        when all fragments are cached. Respects scroll generation counter.

        If any fragment is not cached, falls back to speak_with_local_fallback
        with the full concatenated text.
        """
        if self._muted:
            return

        self._scroll_gen += 1
        my_gen = self._scroll_gen

        # Check if all fragments are cached
        paths: list[str] = []
        all_cached = True
        for frag in fragments:
            key = self._cache_key(frag, voice_override, emotion_override,
                                  speed_override=speed_override)
            path = self._cache.get(key)
            if path and os.path.isfile(path):
                paths.append(path)
            else:
                all_cached = False
                break

        if all_cached and paths:
            # All cached — concatenate and play in background
            def _play():
                if self._scroll_gen != my_gen:
                    return
                combined = self._concat_wavs(paths)
                if not combined:
                    return
                if self._scroll_gen != my_gen:
                    return
                self.stop_sync()
                if self._scroll_gen != my_gen:
                    return
                self._start_playback(combined)
            threading.Thread(target=_play, daemon=True).start()
        else:
            # Not all cached — fall back to full text via speak_with_local_fallback
            full_text = " ".join(fragments)
            # speak_with_local_fallback manages its own scroll_gen
            # so decrement ours to avoid double-increment
            self._scroll_gen -= 1
            self.speak_with_local_fallback(
                full_text, voice_override=voice_override,
                emotion_override=emotion_override,
                speed_override=speed_override)

    def pregenerate(self, texts: list[str],
                    max_workers: int = 0,
                    speed_override: Optional[float] = None) -> None:
        """Generate audio clips for texts in parallel using a thread pool.

        Call this when choices arrive so scrolling is instant.
        Skips API generation when the API is known-broken to avoid
        spawning processes that timeout after 30s.

        Each call increments a generation counter. Workers check this
        before starting each API call and skip if a newer pregenerate()
        has been called (e.g. new choices arrived, making old ones stale).

        Args:
            texts: List of text strings to pregenerate audio for.
            max_workers: Maximum concurrent tts CLI processes. 0 = use config
                         value (config.tts.pregenerateWorkers, default 3).
        """
        # Increment generation — previous workers will detect this and stop
        self._pregen_gen += 1
        my_gen = self._pregen_gen

        # Skip entirely when API is known-broken
        if not self._local and not self._api_gen_available():
            return

        # Filter out already-cached texts
        to_generate = [t for t in texts
                       if self._cache_key(t, speed_override=speed_override) not in self._cache]
        if not to_generate:
            return

        # Determine worker count: explicit arg > config > default 3
        if max_workers <= 0:
            if self._config and hasattr(self._config, 'tts_pregenerate_workers'):
                max_workers = self._config.tts_pregenerate_workers
            else:
                max_workers = 3

        # Generate in parallel using a thread pool.
        # Workers check _pregen_gen before each API call.
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            pool.map(lambda t: self._generate_to_file_unlocked(
                         t, speed_override=speed_override, _pregen_gen=my_gen),
                     to_generate)

    def pregenerate_ui(self, texts: list[str],
                       voice_override: Optional[str] = None,
                       speed_override: Optional[float] = None,
                       max_workers: int = 1) -> None:
        """Pregenerate UI texts (settings, extra options) in a separate queue.

        Uses its own generation counter so UI pregeneration doesn't interfere
        with agent choice pregeneration. Lower default worker count (1) to
        avoid competing for API bandwidth with agent pregenerations.

        Args:
            texts: UI texts to pregenerate (extra option labels, etc.)
            voice_override: Optional voice override (e.g. uiVoice).
            max_workers: Concurrent workers (default 1 for low priority).
        """
        self._pregen_ui_gen += 1
        my_gen = self._pregen_ui_gen

        if not self._local and not self._api_gen_available():
            return

        to_generate = [t for t in texts
                       if self._cache_key(t, voice_override,
                                          speed_override=speed_override) not in self._cache]
        if not to_generate:
            return

        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            pool.map(
                lambda t: self._generate_to_file_unlocked(
                    t, voice_override=voice_override,
                    speed_override=speed_override,
                    _pregen_gen=my_gen,
                    _pregen_counter="_pregen_ui_gen"),
                to_generate)

    def _generate_to_file_unlocked(self, text: str,
                                   voice_override: Optional[str] = None,
                                   emotion_override: Optional[str] = None,
                                   model_override: Optional[str] = None,
                                   speed_override: Optional[float] = None,
                                   _pregen_gen: int = 0,
                                   _pregen_counter: str = "_pregen_gen") -> Optional[str]:
        """Generate audio for text and save to WAV. No locks acquired.

        Used by pregenerate() for parallel generation. Does NOT acquire
        _speech_lock or _api_lock — callers handle concurrency themselves.
        For sequential generation that serializes with playback, use
        _generate_to_file() instead.

        Args:
            _pregen_gen: Generation counter from the pregenerate() call.
                If non-zero, the worker skips generation when a newer
                pregenerate() has been called, avoiding wasted API calls
                for stale choices.
            _pregen_counter: Name of the generation counter attribute to
                check against. Default "_pregen_gen" for agent pregeneration,
                "_pregen_ui_gen" for UI pregeneration.
        """
        # Check staleness — skip if a newer pregenerate() has been called
        if _pregen_gen and getattr(self, _pregen_counter, 0) > _pregen_gen:
            return None

        key = self._cache_key(text, voice_override, emotion_override,
                              model_override=model_override,
                              speed_override=speed_override)

        # Check cache
        cached = self._cache.get(key)
        if cached and os.path.isfile(cached):
            return cached

        out_path = os.path.join(CACHE_DIR, f"{key}.wav")

        try:
            if self._local:
                if not self._espeak:
                    return None
                wpm = int(TTS_SPEED * self._speed)
                cmd = [self._espeak, "--stdout", "-s", str(wpm), text]
                with open(out_path, "wb") as f:
                    proc = subprocess.run(
                        cmd, stdout=f, stderr=subprocess.PIPE,
                        env=self._env, timeout=10,
                    )
                if proc.returncode != 0:
                    stderr_out = (proc.stderr or b"").decode("utf-8", errors="replace").strip()
                    self._log_tts_error(
                        f"espeak-ng failed (code {proc.returncode}): {stderr_out}", text)
                    return None
            else:
                if not self._tts_bin:
                    return None

                if not self._api_gen_available():
                    return None

                # Re-check staleness before expensive API call
                if _pregen_gen and getattr(self, _pregen_counter, 0) > _pregen_gen:
                    return None

                if self._config:
                    cmd = [self._tts_bin] + self._config.tts_cli_args(
                        text, voice_override=voice_override,
                        emotion_override=emotion_override,
                        model_override=model_override,
                        speed_override=speed_override)
                else:
                    cmd = [self._tts_bin, text, "--stdout", "--response-format", "wav"]

                with open(out_path, "wb") as f:
                    proc = subprocess.run(
                        cmd, stdout=f, stderr=subprocess.PIPE,
                        env=self._env, timeout=15,
                    )
                if proc.returncode != 0:
                    # Signal kill = intentional cancellation, not an error
                    if proc.returncode < 0:
                        _log.debug("Pregenerate cancelled by signal %d: %s",
                                   -proc.returncode, text[:60])
                    else:
                        stderr_out = (proc.stderr or b"").decode("utf-8", errors="replace").strip()
                        self._log_tts_error(
                            f"tts CLI failed (code {proc.returncode}): {stderr_out}", text)
                        self._record_api_gen_failure()
                    try:
                        os.unlink(out_path)
                    except OSError:
                        pass
                    return None

                try:
                    fsize = os.path.getsize(out_path)
                    if fsize < 44:
                        self._log_tts_error(
                            f"tts CLI produced invalid WAV ({fsize} bytes)", text)
                        self._record_api_gen_failure()
                        try:
                            os.unlink(out_path)
                        except OSError:
                            pass
                        return None
                except OSError:
                    pass

            self._cache[key] = out_path
            self._record_api_gen_success()
            return out_path

        except subprocess.TimeoutExpired:
            self._log_tts_error("TTS generation timed out", text)
            if not self._local:
                self._record_api_gen_failure()
            return None
        except Exception as e:
            self._log_tts_error(f"TTS generation exception: {e}", text)
            if not self._local:
                self._record_api_gen_failure()
            return None

    def play_cached(self, text: str, block: bool = False,
                    voice_override: Optional[str] = None,
                    emotion_override: Optional[str] = None,
                    model_override: Optional[str] = None,
                    speed_override: Optional[float] = None) -> None:
        """Play a pregenerated audio clip. Falls back to live generation.

        If block=True, waits for playback to finish before returning.
        If block=False, starts playback and returns immediately.

        Does NOT stop current speech — callers use the _speech_lock to
        serialize playback. Scroll/select overlay uses stop_sync() directly.
        """
        if not self._paplay or self._muted:
            return

        key = self._cache_key(text, voice_override, emotion_override,
                              model_override=model_override,
                              speed_override=speed_override)
        path = self._cache.get(key)

        if path and os.path.isfile(path):
            _time_mod.sleep(0.05)  # Brief pause to let PulseAudio settle
            if not self._start_playback(path, max_attempts=self._max_retries):
                self._log_tts_error("paplay failed for cached audio", text)
            elif block:
                self._wait_for_playback()
        else:
            # Slow path: generate on demand
            p = self._generate_to_file(text, voice_override, emotion_override,
                                       model_override=model_override,
                                       speed_override=speed_override)
            if p:
                _time_mod.sleep(0.05)  # Brief pause to let PulseAudio settle
                if not self._start_playback(p, max_attempts=self._max_retries):
                    self._log_tts_error("paplay failed after generation", text)
                elif block:
                    self._wait_for_playback()
            else:
                # API generation failed — report error (no local fallback)
                self._report_tts_error(f"TTS generation failed for: {text[:60]}")

    def _report_tts_error(self, message: str) -> None:
        """Report a TTS error via the error callback (if set).

        Called when API TTS fails and local fallback is disabled.
        The TUI registers a callback to show errors visually.

        When the API is known-broken (past the failure threshold), suppress
        both logging and the visual callback to avoid flooding the user with
        hundreds of identical "TTS API unavailable" red error messages.
        """
        if not self._api_gen_available():
            # API is known-broken — don't spam ERROR log or TUI for every single
            # speech attempt. The initial failures were already logged.
            _log.debug("TTS suppressed (API unavailable): %s", message[:80])
            return
        self._log_tts_error(message)
        cb = getattr(self, '_on_tts_error', None)
        if cb:
            try:
                cb(message)
            except Exception:
                pass

    def _local_tts_fallback(self, text: str) -> None:
        """Fall back to local TTS when API TTS fails (e.g. missing API key).

        Disabled by default — reports an error instead of falling back.
        Only uses local backends (termux/espeak) when in --local mode.
        """
        if not self._local:
            # Not in local mode — report error instead of falling back
            self._report_tts_error(f"TTS failed: {text[:60]}")
            return
        try:
            if self._local_backend == "termux" and self._termux_exec:
                self._speak_termux(text)
            elif self._local_backend == "espeak" and self._espeak and self._paplay:
                # espeak file-based fallback — only when espeak is the configured backend
                wpm = int(TTS_SPEED * self._speed)
                cmd = [self._espeak, "--stdout", "-s", str(wpm), text]
                proc = subprocess.run(
                    cmd, capture_output=True, timeout=10,
                )
                if proc.returncode == 0 and len(proc.stdout) > 44:
                    # Write to temp file and play
                    import tempfile
                    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
                        f.write(proc.stdout)
                        tmp_path = f.name
                    self.stop_sync()
                    self._start_playback(tmp_path)
        except Exception:
            pass

    def _start_playback(self, path: str, max_attempts: int = 0) -> bool:
        """Start paplay for a WAV file. Returns True if playback started ok.

        Detects immediate paplay failures (e.g. PulseAudio connection refused)
        and retries up to max_attempts times (default: self._max_retries).
        Use max_attempts=0 for scroll readout where speed matters more than
        reliability.

        IMPORTANT: Popen is called via the subprocess manager which handles
        process group setup (preexec_fn=os.setsid) and tracking automatically.
        No lock is needed — the manager uses GIL-atomic list operations.
        """
        if max_attempts < 0:
            max_attempts = self._max_retries
        for attempt in range(1 + max_attempts):
            try:
                tracked = self._mgr.start(
                    [self._paplay, path],
                    tag="playback",
                    env=self._env,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                )
                proc = tracked.proc
            except Exception as e:
                self._record_failure(f"Failed to start paplay: {e}")
                continue

            # Give paplay a moment to fail on connection errors
            try:
                retcode = proc.wait(timeout=0.15)
                # Process exited immediately — likely a connection error
                stderr_out = ""
                try:
                    stderr_out = (proc.stderr.read() or b"").decode("utf-8", errors="replace").strip()
                except Exception:
                    pass
                if retcode != 0:
                    # Negative return code = killed by signal (intentional stop)
                    if retcode < 0:
                        return False  # Don't retry — was killed intentionally
                    self._record_failure(
                        f"paplay exited immediately (code {retcode}): {stderr_out or 'no stderr'}"
                    )
                    # Brief pause before retry to let PulseAudio settle
                    if attempt < max_attempts:
                        _time_mod.sleep(0.1 * (attempt + 1))
                    continue
            except subprocess.TimeoutExpired:
                # Still running after 0.15s — playback started successfully
                pass

            # Success
            self._total_plays += 1
            if self._consecutive_failures > 0:
                self._log_recovery(attempt)
            self._consecutive_failures = 0
            return True

        # All retries exhausted
        return False

    def _wait_for_playback(self) -> None:
        """Wait for current playback to finish. Logs errors on failure."""
        tracked = self._mgr.get_by_tag("playback")
        if tracked is not None:
            proc = tracked.proc
            try:
                retcode = proc.wait(timeout=30)
                if retcode != 0:
                    # Negative return code = killed by signal (intentional stop)
                    if retcode < 0:
                        return
                    stderr_out = ""
                    try:
                        stderr_out = (proc.stderr.read() or b"").decode("utf-8", errors="replace").strip()
                    except Exception:
                        pass
                    self._record_failure(
                        f"paplay exited with code {retcode}: {stderr_out or 'no stderr'}"
                    )
            except subprocess.TimeoutExpired:
                self._record_failure("paplay timed out after 30s")
            except Exception:
                pass

    def speak(self, text: str, voice_override: Optional[str] = None,
              emotion_override: Optional[str] = None,
              model_override: Optional[str] = None,
              speed_override: Optional[float] = None) -> None:
        """Speak text and BLOCK until playback finishes.

        Acquires the speech lock to ensure sequential playback — no
        self-interruption. Previous speech finishes before this starts.
        """
        with self._speech_lock:
            if self._local and self._local_backend == "termux" and self._termux_exec:
                self._speak_termux(text)
                return
            # Check cache first — if cached, use play_cached (instant)
            key = self._cache_key(text, voice_override, emotion_override,
                                  model_override=model_override,
                                  speed_override=speed_override)
            cached = self._cache.get(key)
            if cached and os.path.isfile(cached):
                self.play_cached(text, block=True, voice_override=voice_override,
                                emotion_override=emotion_override,
                                model_override=model_override,
                                speed_override=speed_override)
            elif not self._local and self._tts_bin and self._config:
                self.speak_streaming(text, voice_override=voice_override,
                                   emotion_override=emotion_override,
                                   model_override=model_override,
                                   speed_override=speed_override,
                                   block=True)
            else:
                self.play_cached(text, block=True, voice_override=voice_override,
                                emotion_override=emotion_override,
                                model_override=model_override,
                                speed_override=speed_override)

    def speak_async(self, text: str, voice_override: Optional[str] = None,
                    emotion_override: Optional[str] = None,
                    model_override: Optional[str] = None,
                    speed_override: Optional[float] = None) -> None:
        """Speak text without blocking. Queues behind any current speech.

        Acquires the speech lock in a background thread to ensure
        sequential playback — speech queues up naturally without
        interrupting what's currently playing.
        """
        def _do():
            try:
                with self._speech_lock:
                    if self._local and self._local_backend == "termux" and self._termux_exec:
                        self._speak_termux(text)
                        return
                    key = self._cache_key(text, voice_override, emotion_override,
                                          model_override=model_override,
                                          speed_override=speed_override)
                    cached = self._cache.get(key)
                    if cached and os.path.isfile(cached):
                        self.play_cached(text, block=True, voice_override=voice_override,
                                       emotion_override=emotion_override,
                                       model_override=model_override,
                                       speed_override=speed_override)
                    elif not self._local and self._tts_bin and self._config:
                        self.speak_streaming(text, voice_override=voice_override,
                                           emotion_override=emotion_override,
                                           model_override=model_override,
                                           speed_override=speed_override,
                                           block=True)
                    else:
                        self.play_cached(text, block=True, voice_override=voice_override,
                                       emotion_override=emotion_override,
                                       model_override=model_override,
                                       speed_override=speed_override)
            except Exception as e:
                _log.error("speak_async error", exc_info=True)
        threading.Thread(target=_do, daemon=True).start()

    def is_cached(self, text: str, voice_override: Optional[str] = None,
                  emotion_override: Optional[str] = None,
                  speed_override: Optional[float] = None) -> bool:
        """Check if audio for this text is already generated."""
        key = self._cache_key(text, voice_override, emotion_override,
                              speed_override=speed_override)
        path = self._cache.get(key)
        return path is not None and os.path.isfile(path)

    def _speak_termux(self, text: str, block: bool = True) -> None:
        """Speak text via termux-tts-speak (Android native TTS).

        Uses the MUSIC audio stream for nice output quality.

        Args:
            block: If True, wait for speech to finish. If False, fire-and-forget.
                   Use block=False for scroll readout where speed matters.
        """
        if not self._termux_exec:
            return

        # Kill any previous termux-tts-speak via the manager
        self._mgr.cancel_tagged("termux")

        try:
            speed = self._config.tts_speed if self._config else self._speed
            cmd = [self._termux_exec, "termux-tts-speak",
                   "-s", "MUSIC", "-r", str(speed), text]
            tracked = self._mgr.start(
                cmd,
                tag="termux",
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            if block:
                try:
                    tracked.proc.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    tracked.kill()
        except Exception:
            pass

    def _kill_termux_proc(self) -> None:
        """Kill the current termux-tts-speak process and its entire process group."""
        self._mgr.cancel_tagged("termux")

    def speak_with_local_fallback(self, text: str,
                                   voice_override: Optional[str] = None,
                                   emotion_override: Optional[str] = None,
                                   nonblocking: bool = False,
                                   speed_override: Optional[float] = None) -> None:
        """Speak text for scroll readout: cached audio preferred, API fallback.

        On cache hit: plays immediately in a background thread.
        On cache miss (API mode): calls speak_async() to generate + play via API.
        On cache miss (local mode): uses configured local backend (termux/espeak).

        This keeps the scroll path fast — cache hits play instantly, cache misses
        trigger async API generation which will cache for next time.

        In --local mode only, uses configured local backend (termux/espeak).
        espeak is NEVER used in non-local mode.

        Args:
            nonblocking: If True, used for freeform text entry readback.
                In API mode, uses speak_async(). In local mode, uses local backend.
        """
        if self._muted:
            return

        # Increment scroll generation — background threads check this
        # before playing to prevent stale audio from overlapping.
        self._scroll_gen += 1
        my_gen = self._scroll_gen

        key = self._cache_key(text, voice_override, emotion_override,
                              speed_override=speed_override)
        path = self._cache.get(key)

        if path and os.path.isfile(path):
            # Cache hit — play the full quality version in background thread
            # to avoid blocking the main Textual event loop.
            def _play_cached():
                if self._scroll_gen != my_gen:
                    return  # stale — newer scroll superseded us
                self.stop_sync()
                if self._scroll_gen != my_gen:
                    return  # stale — newer scroll superseded us
                self._start_playback(path)
            threading.Thread(target=_play_cached, daemon=True).start()
            return

        # --local mode: use configured local backend (espeak/termux)
        if self._local:
            if self._local_backend == "termux" and self._termux_exec:
                def _termux_play():
                    if self._scroll_gen != my_gen:
                        return
                    self.stop_sync()
                    self._speak_termux(text, block=False)
                threading.Thread(target=_termux_play, daemon=True).start()
            elif self._local_backend == "espeak" and self._espeak and self._paplay:
                def _espeak_local():
                    try:
                        if self._scroll_gen != my_gen:
                            return
                        wpm = int(TTS_SPEED * self._speed)
                        tmp = os.path.join(CACHE_DIR, f"_espeak_{hashlib.md5(text.encode()).hexdigest()[:8]}.wav")
                        with open(tmp, "wb") as f:
                            subprocess.run(
                                [self._espeak, "--stdout", "-s", str(wpm), text],
                                stdout=f, stderr=subprocess.DEVNULL,
                                env=self._env, timeout=5,
                            )
                        if self._scroll_gen != my_gen:
                            return
                        self.stop_sync()
                        self._start_playback(tmp)
                    except Exception:
                        pass
                threading.Thread(target=_espeak_local, daemon=True).start()
            return

        # Cache miss, API mode — use speak_async() to generate and play.
        # This uses the API TTS voice (not espeak) and caches for next time.
        self.speak_async(text, voice_override=voice_override,
                         emotion_override=emotion_override,
                         speed_override=speed_override)

    def speak_streaming(self, text: str, voice_override: Optional[str] = None,
                        emotion_override: Optional[str] = None,
                        model_override: Optional[str] = None,
                        speed_override: Optional[float] = None,
                        block: bool = True) -> None:
        """Speak text by piping tts stdout directly to paplay (no file).

        This reduces time-to-first-audio because playback starts as soon
        as the TTS service sends initial WAV data, rather than waiting for
        the entire response. Falls back to cached play if streaming is
        unavailable (local mode or missing binaries).

        Retries up to 2 times on API errors (HTTP 500, timeouts) with
        exponential backoff (1s, 2s). Signal kills and intentional
        cancellations are not retried.

        Args:
            text: Text to speak.
            voice_override: Optional voice name for per-session rotation.
            emotion_override: Optional emotion preset for per-session rotation.
            model_override: Optional model name for cross-provider rotation.
            speed_override: Optional speed multiplier override.
            block: If True, wait for playback to finish before returning.
        """
        max_retries = 2
        for attempt in range(max_retries + 1):
            result = self._speak_streaming_once(
                text, voice_override=voice_override,
                emotion_override=emotion_override,
                model_override=model_override,
                speed_override=speed_override, block=block)
            if result != "retry":
                return
            # Exponential backoff: 1s, 2s
            delay = 2 ** attempt
            _log.info("TTS streaming retry %d/%d in %ds: %s",
                      attempt + 1, max_retries, delay, text[:60])
            _time_mod.sleep(delay)
        # All retries exhausted — fall back to non-streaming
        _log.warning("TTS streaming failed after %d retries, falling back: %s",
                     max_retries, text[:60])
        self.play_cached(text, block=block, voice_override=voice_override,
                        emotion_override=emotion_override,
                        model_override=model_override,
                        speed_override=speed_override)

    def _speak_streaming_once(self, text: str, voice_override: Optional[str] = None,
                              emotion_override: Optional[str] = None,
                              model_override: Optional[str] = None,
                              speed_override: Optional[float] = None,
                              block: bool = True) -> Optional[str]:
        """Single attempt at streaming TTS. Returns "retry" if retriable error.

        Returns None on success or non-retriable failure, "retry" on API error.
        """
        if not self._paplay or self._muted:
            return

        # Check cache first — if we have a cached file, no need to stream
        key = self._cache_key(text, voice_override, emotion_override,
                              model_override=model_override,
                              speed_override=speed_override)
        cached = self._cache.get(key)
        if cached and os.path.isfile(cached):
            self._start_playback(cached, max_attempts=self._max_retries)
            if block:
                self._wait_for_playback()
            return

        # Streaming only works with API tts backend (not espeak-ng local)
        if self._local or not self._tts_bin or not self._config:
            # Use configured local backend when in local mode
            if self._local and self._local_backend == "termux" and self._termux_exec:
                self._speak_termux(text)
                return
            # Fall back to non-streaming (espeak file generation)
            self.play_cached(text, block=block, voice_override=voice_override,
                           emotion_override=emotion_override,
                           model_override=model_override,
                           speed_override=speed_override)
            return

        # Skip streaming when API is known-broken — report error
        if not self._api_gen_available():
            self._report_tts_error(f"TTS API unavailable: {text[:60]}")
            return

        # Build tts command
        cmd = [self._tts_bin] + self._config.tts_cli_args(
            text, voice_override=voice_override,
            emotion_override=emotion_override,
            model_override=model_override,
            speed_override=speed_override)

        try:
            # Start TTS process first and verify we get a valid WAV header
            # before connecting paplay. This prevents "Failed to open audio
            # file" errors when the TTS API is slow or errors out.
            tts_tracked = self._mgr.start(
                cmd,
                tag="tts_stream",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=self._env,
            )
            tts_proc = tts_tracked.proc

            # Read the WAV header (44 bytes) with a timeout.
            # If TTS fails or is too slow, we bail early instead of
            # giving paplay an empty/corrupt stream.
            import select
            header = b""
            deadline = _time_mod.time() + 10  # 10s to get WAV header
            while len(header) < 44 and _time_mod.time() < deadline:
                ready, _, _ = select.select([tts_proc.stdout], [], [], 0.5)
                if ready:
                    chunk = tts_proc.stdout.read(44 - len(header))
                    if not chunk:
                        break  # EOF — TTS process closed stdout
                    header += chunk
                # Check if TTS process died
                if tts_proc.poll() is not None and not ready:
                    break

            if len(header) < 44 or header[:4] != b"RIFF":
                # Check if the process was killed by a signal (intentional cancellation)
                # e.g. cancel_all() from stop()/stop_sync() during scroll or new speech
                tts_rc = tts_proc.poll()
                if tts_rc is not None and tts_rc < 0:
                    # Killed by signal — intentional interruption, not an error
                    _log.debug("TTS streaming cancelled by signal %d: %s",
                               -tts_rc, text[:60])
                    return None
                # Also check if the process is no longer tracked by the manager
                # (cancel_all() removes it from tracking before killing)
                if not tts_tracked.alive and tts_rc is not None:
                    # Process was killed/died — check if it was a signal
                    # rc < 0: Unix signal, 137: SIGKILL (128+9), 143: SIGTERM (128+15)
                    if tts_rc < 0 or tts_rc in (137, 143):
                        _log.debug("TTS streaming cancelled (rc=%d): %s",
                                   tts_rc, text[:60])
                        return None

                # TTS failed to produce valid WAV — get diagnostics
                tts_stderr = ""
                try:
                    tts_proc.wait(timeout=2)
                    tts_stderr = (tts_proc.stderr.read() or b"").decode("utf-8", errors="replace").strip()
                except Exception:
                    pass

                # 0 bytes + empty stderr = most likely killed/cancelled
                # (the process never had a chance to produce output or error)
                if len(header) == 0 and not tts_stderr:
                    _log.debug("TTS streaming likely cancelled (0 bytes, no stderr): %s",
                               text[:60])
                    return None

                self._log_tts_error(
                    f"tts CLI produced no/invalid WAV header ({len(header)} bytes): {tts_stderr[:120]}",
                    text)
                self._record_api_gen_failure()
                # Check if this is a retriable API error (500, timeout, etc.)
                if "500" in tts_stderr or "Internal Server Error" in tts_stderr:
                    return "retry"
                self._report_tts_error(f"TTS streaming failed: {tts_stderr[:80]}")
                return None

            # Header looks valid — start paplay with a relay thread that
            # feeds the header we already read + the rest of tts stdout.
            play_tracked = self._mgr.start(
                [self._paplay],
                tag="playback",
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                env=self._env,
            )
            play_proc = play_tracked.proc

            def _relay():
                """Relay header + remaining tts stdout to paplay stdin."""
                try:
                    play_proc.stdin.write(header)
                    while True:
                        chunk = tts_proc.stdout.read(4096)
                        if not chunk:
                            break
                        play_proc.stdin.write(chunk)
                except (BrokenPipeError, OSError):
                    pass
                finally:
                    try:
                        play_proc.stdin.close()
                    except Exception:
                        pass
                    try:
                        tts_proc.stdout.close()
                    except Exception:
                        pass

            relay_thread = threading.Thread(target=_relay, daemon=True)
            relay_thread.start()

            if block:
                tts_failed = False
                try:
                    retcode = play_proc.wait(timeout=60)
                    if retcode != 0:
                        # Negative return code = killed by signal (e.g. stop())
                        # This is intentional interruption, not a failure
                        if retcode < 0:
                            pass  # Signal kill — don't log or count
                        else:
                            stderr_out = ""
                            try:
                                stderr_out = (play_proc.stderr.read() or b"").decode("utf-8", errors="replace").strip()
                            except Exception:
                                pass
                            # Also grab TTS process stderr for combined diagnostics
                            tts_diag = ""
                            try:
                                tts_rc = tts_proc.wait(timeout=2)
                                if tts_rc != 0:
                                    tts_err = (tts_proc.stderr.read() or b"").decode("utf-8", errors="replace").strip()
                                    tts_diag = f" | tts exit={tts_rc}: {tts_err[:120]}" if tts_err else f" | tts exit={tts_rc}"
                            except Exception:
                                pass
                            self._record_failure(
                                f"paplay (streaming) exited with code {retcode}: {stderr_out or 'no stderr'}{tts_diag}"
                            )
                            tts_failed = True
                    else:
                        self._total_plays += 1
                        self._consecutive_failures = 0
                        self._record_api_gen_success()  # Reset API failure tracking
                except subprocess.TimeoutExpired:
                    tts_failed = True
                except Exception:
                    pass
                # Check if tts itself failed (e.g. API error)
                try:
                    tts_retcode = tts_proc.wait(timeout=5)
                    if tts_retcode != 0:
                        # Negative return code = killed by signal (intentional cancel)
                        if tts_retcode < 0:
                            _log.debug("TTS streaming tts_proc cancelled by signal %d: %s",
                                       -tts_retcode, text[:60])
                        else:
                            tts_stderr = ""
                            try:
                                tts_stderr = (tts_proc.stderr.read() or b"").decode("utf-8", errors="replace").strip()
                            except Exception:
                                pass
                            if tts_stderr:
                                self._log_tts_error(
                                    f"tts CLI (streaming) failed (code {tts_retcode}): {tts_stderr}",
                                    text)
                            tts_failed = True
                            self._record_api_gen_failure()
                except Exception:
                    pass
                # Report error if streaming failed (no local fallback)
                if tts_failed:
                    self._report_tts_error(f"TTS streaming failed: {text[:60]}")
                # Non-blocking: monitor in background thread for error reporting
                def _monitor():
                    tts_failed = False
                    try:
                        retcode = play_proc.wait(timeout=60)
                        if retcode != 0:
                            # Negative return code = killed by signal (e.g. stop())
                            # This is intentional interruption, not a failure
                            if retcode < 0:
                                pass  # Signal kill — don't log or count
                            else:
                                stderr_out = ""
                                try:
                                    stderr_out = (play_proc.stderr.read() or b"").decode("utf-8", errors="replace").strip()
                                except Exception:
                                    pass
                                # Also grab TTS process stderr for combined diagnostics
                                tts_diag = ""
                                try:
                                    tts_rc = tts_proc.wait(timeout=2)
                                    if tts_rc != 0:
                                        tts_err = (tts_proc.stderr.read() or b"").decode("utf-8", errors="replace").strip()
                                        tts_diag = f" | tts exit={tts_rc}: {tts_err[:120]}" if tts_err else f" | tts exit={tts_rc}"
                                except Exception:
                                    pass
                                self._record_failure(
                                    f"paplay (streaming async) exited with code {retcode}: {stderr_out or 'no stderr'}{tts_diag}"
                                )
                                tts_failed = True
                        else:
                            self._total_plays += 1
                            self._consecutive_failures = 0
                            self._record_api_gen_success()  # Reset API failure tracking
                    except subprocess.TimeoutExpired:
                        tts_failed = True
                    except Exception:
                        pass
                    # Check if tts itself failed
                    try:
                        tts_retcode = tts_proc.wait(timeout=5)
                        if tts_retcode != 0:
                            # Negative return code = killed by signal (intentional cancel)
                            if tts_retcode < 0:
                                _log.debug("TTS streaming async tts_proc cancelled by signal %d: %s",
                                           -tts_retcode, text[:60])
                            else:
                                tts_stderr = ""
                                try:
                                    tts_stderr = (tts_proc.stderr.read() or b"").decode("utf-8", errors="replace").strip()
                                except Exception:
                                    pass
                                if tts_stderr:
                                    self._log_tts_error(
                                        f"tts CLI (streaming async) failed (code {tts_retcode}): {tts_stderr}",
                                        text)
                                tts_failed = True
                                self._record_api_gen_failure()
                    except Exception:
                        pass
                    # Report error if streaming failed (no local fallback)
                    if tts_failed:
                        self._report_tts_error(f"TTS streaming async failed: {text[:60]}")
                threading.Thread(target=_monitor, daemon=True).start()
        except Exception as e:
            self._record_failure(f"Streaming TTS setup failed: {e}")
            return "retry"

    def speak_streaming_async(self, text: str, voice_override: Optional[str] = None,
                              emotion_override: Optional[str] = None,
                              model_override: Optional[str] = None) -> None:
        """Speak text via streaming pipe without blocking caller."""
        def _do():
            self.speak_streaming(text, voice_override=voice_override,
                               emotion_override=emotion_override,
                               model_override=model_override, block=False)
        threading.Thread(target=_do, daemon=True).start()

    def stop(self) -> None:
        """Kill any in-progress playback and streaming TTS.

        Runs cancel_all() in a background thread to avoid blocking
        the caller (important on proot where syscalls take 10-50ms each).
        The subprocess manager handles process group killing internally.
        """
        def _do_stop():
            self._mgr.cancel_all()
        threading.Thread(target=_do_stop, daemon=True).start()

    def stop_sync(self) -> None:
        """Kill any in-progress playback synchronously (blocks caller).

        Use this only when you need to guarantee audio is stopped before
        proceeding (e.g., before starting blocking speech in a background
        thread). Prefer stop() for UI/event-loop contexts.
        """
        self._mgr.cancel_all()

    def wait_for_speech(self, timeout: float = 5.0) -> None:
        """Wait for any in-progress speech to finish, up to timeout seconds.

        Used before starting new speech that should follow (not interrupt)
        the current speech. For example, choice presentation waits for any
        prior speak_async to finish before reading the intro.
        """
        deadline = _time_mod.time() + timeout
        while _time_mod.time() < deadline:
            if not self._mgr.has_active():
                return
            _time_mod.sleep(0.1)

    def mute(self) -> None:
        """Stop playback and prevent any new audio from playing."""
        self._muted = True
        self.stop_sync()

    def unmute(self) -> None:
        """Allow audio playback again."""
        self._muted = False

    def clear_cache(self) -> None:
        """Remove all cached audio files."""
        self._cache.clear()
        try:
            shutil.rmtree(CACHE_DIR, ignore_errors=True)
            os.makedirs(CACHE_DIR, exist_ok=True)
        except Exception:
            pass

    def reconnect_pulse(self) -> tuple[bool, str]:
        """Attempt to reconnect PulseAudio with gentle recovery strategies.

        Tries only non-destructive strategies:
        1. pactl info (check if it's already back)
        2. pulseaudio --start (start daemon if not running)

        Returns (success, diagnostic_info) tuple:
            success: True if PulseAudio is reachable after reconnection attempts.
            diagnostic_info: String with diagnostic details for logging/notifications.
        """
        env = self._env.copy()
        pactl = _find_binary("pactl")
        pulseaudio = _find_binary("pulseaudio")
        pulse_server = env.get("PULSE_SERVER", "127.0.0.1")
        diagnostics: list[str] = []

        # Use a longer timeout for remote PulseAudio (over Tailscale etc.)
        is_remote = pulse_server not in ("127.0.0.1", "localhost", "")
        pactl_timeout = 8 if is_remote else 3

        # No pactl binary → can't check PulseAudio at all
        if not pactl:
            return False, "pactl binary not found"

        # Strategy 1: Check if PulseAudio is already back
        try:
            result = subprocess.run(
                [pactl, "info"],
                env=env, capture_output=True, timeout=pactl_timeout,
            )
            if result.returncode == 0:
                return True, "PulseAudio was already reachable"
            diagnostics.append(f"pactl info failed (rc={result.returncode})")
        except subprocess.TimeoutExpired:
            diagnostics.append(f"pactl info timed out ({pactl_timeout}s)")
        except Exception as e:
            diagnostics.append(f"pactl info error: {e}")

        # Strategy 2: Try to start PulseAudio daemon (non-destructive)
        if pulseaudio:
            try:
                result = subprocess.run(
                    [pulseaudio, "--start"],
                    env=env, capture_output=True, timeout=5,
                )
                if result.returncode == 0:
                    diagnostics.append("pulseaudio --start succeeded")
                else:
                    stderr = result.stderr.decode("utf-8", errors="replace").strip()
                    diagnostics.append(f"pulseaudio --start failed: {stderr[:100]}")
            except subprocess.TimeoutExpired:
                diagnostics.append("pulseaudio --start timed out")
            except Exception as e:
                diagnostics.append(f"pulseaudio --start error: {e}")

            # Check after daemon start — give it a moment for remote
            if is_remote:
                _time_mod.sleep(1)

            try:
                result = subprocess.run(
                    [pactl, "info"],
                    env=env, capture_output=True, timeout=pactl_timeout,
                )
                if result.returncode == 0:
                    return True, "; ".join(diagnostics + ["recovered after daemon start"])
            except Exception:
                pass

        # All strategies failed
        diagnostics.append(f"PULSE_SERVER={pulse_server}")
        return False, "; ".join(diagnostics)

    def pulse_recovery_steps(self) -> list[str]:
        """Return specific user-facing recovery steps for PulseAudio failures.

        Called when auto-reconnect is exhausted to provide actionable guidance.
        """
        pulse_server = self._env.get("PULSE_SERVER", "127.0.0.1")
        steps = [
            f"Check PULSE_SERVER ({pulse_server}) is reachable: pactl -s {pulse_server} info",
            "Restart PulseAudio daemon: pulseaudio --kill && pulseaudio --start",
        ]
        if pulse_server not in ("127.0.0.1", "localhost"):
            steps.append(
                f"Check network connectivity: ping {pulse_server}"
            )
            steps.append(
                "Ensure PulseAudio TCP module is loaded on remote: "
                "pactl load-module module-native-protocol-tcp"
            )
        steps.append("Restart io-mcp TUI to reset audio subsystem")
        return steps

    def cleanup(self) -> None:
        self.stop_sync()
        self.clear_cache()

    # ─── Audio cues (tone generation) ─────────────────────────────

    def play_tone(self, frequency: float = 800, duration_ms: int = 100,
                  volume: float = 0.3, fade: bool = True) -> None:
        """Play a simple sine wave tone (non-blocking).

        Generates a WAV in memory and plays via paplay.
        Used for UI audio cues (chimes, clicks, etc.).

        Args:
            frequency: Tone frequency in Hz (default 800)
            duration_ms: Duration in milliseconds (default 100)
            volume: Volume 0.0-1.0 (default 0.3)
            fade: Apply fade-in/out to avoid clicks (default True)
        """
        if not self._paplay or self._muted:
            return
        # Check if chimes are enabled in config
        if self._config and not self._config.chimes_enabled:
            return

        import math
        import struct
        import io

        sample_rate = 24000
        num_samples = int(sample_rate * duration_ms / 1000)

        # Generate sine wave
        samples = []
        for i in range(num_samples):
            t = i / sample_rate
            val = math.sin(2 * math.pi * frequency * t) * volume

            # Fade in/out to avoid clicks
            if fade:
                fade_samples = min(num_samples // 5, 200)
                if i < fade_samples:
                    val *= i / fade_samples
                elif i > num_samples - fade_samples:
                    val *= (num_samples - i) / fade_samples

            samples.append(int(val * 32767))

        # Build WAV in memory
        raw_audio = struct.pack(f'<{num_samples}h', *samples)

        # WAV header
        wav = io.BytesIO()
        data_size = len(raw_audio)
        wav.write(b'RIFF')
        wav.write(struct.pack('<I', 36 + data_size))
        wav.write(b'WAVE')
        wav.write(b'fmt ')
        wav.write(struct.pack('<IHHIIHH', 16, 1, 1, sample_rate,
                              sample_rate * 2, 2, 16))
        wav.write(b'data')
        wav.write(struct.pack('<I', data_size))
        wav.write(raw_audio)

        # Write to temp file and play
        tone_path = os.path.join(CACHE_DIR, f"tone-{int(frequency)}-{duration_ms}.wav")
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(tone_path, 'wb') as f:
            f.write(wav.getvalue())

        # Play without stopping current speech
        try:
            subprocess.Popen(
                [self._paplay, tone_path],
                env=self._env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            pass

    def play_chime(self, style: str = "choices") -> None:
        """Play a predefined audio cue.

        Styles:
            choices: Two ascending tones (new choices arrived)
            select: Short low click (selection made)
            connect: Three-note ascending (agent connected)
            record_start: Rising tone (recording started)
            record_stop: Falling tone (recording stopped)
            convo_on: Two ascending (conversation mode on)
            convo_off: Two descending (conversation mode off)
            urgent: Sharp attention-grabbing discord (critical alert)
            error: Low pulsing tone (something went wrong)
            warning: Mid-frequency double pulse (caution)
            success: Bright ascending arpeggio (task completed)
            disconnect: Descending three-note (agent disconnected)
            inbox: Quick triple ascending (new inbox item queued)
        """
        if self._muted:
            return

        def _play():
            if style == "choices":
                self.play_tone(600, 60, 0.15)
                _time_mod.sleep(0.08)
                self.play_tone(900, 80, 0.2)
            elif style == "select":
                self.play_tone(400, 40, 0.2)
            elif style == "connect":
                self.play_tone(500, 50, 0.15)
                _time_mod.sleep(0.06)
                self.play_tone(700, 50, 0.15)
                _time_mod.sleep(0.06)
                self.play_tone(900, 70, 0.2)
            elif style == "record_start":
                self.play_tone(400, 60, 0.2)
                _time_mod.sleep(0.05)
                self.play_tone(800, 80, 0.25)
            elif style == "record_stop":
                self.play_tone(800, 60, 0.2)
                _time_mod.sleep(0.05)
                self.play_tone(400, 80, 0.15)
            elif style == "convo_on":
                self.play_tone(500, 50, 0.15)
                _time_mod.sleep(0.06)
                self.play_tone(800, 70, 0.2)
            elif style == "convo_off":
                self.play_tone(800, 50, 0.15)
                _time_mod.sleep(0.06)
                self.play_tone(500, 70, 0.15)
            elif style == "urgent":
                # Sharp attention-grabber: high-frequency discord
                self.play_tone(1200, 80, 0.35)
                _time_mod.sleep(0.06)
                self.play_tone(800, 80, 0.35)
                _time_mod.sleep(0.06)
                self.play_tone(1200, 80, 0.35)
            elif style == "error":
                # Low pulsing tone — something went wrong
                self.play_tone(250, 120, 0.3)
                _time_mod.sleep(0.1)
                self.play_tone(200, 150, 0.25)
            elif style == "warning":
                # Mid-frequency double pulse — caution
                self.play_tone(600, 60, 0.25)
                _time_mod.sleep(0.12)
                self.play_tone(600, 60, 0.25)
            elif style == "success":
                # Bright ascending arpeggio — task completed
                self.play_tone(600, 50, 0.2)
                _time_mod.sleep(0.05)
                self.play_tone(800, 50, 0.2)
                _time_mod.sleep(0.05)
                self.play_tone(1000, 50, 0.2)
                _time_mod.sleep(0.05)
                self.play_tone(1200, 80, 0.25)
            elif style == "disconnect":
                # Descending three-note — agent gone
                self.play_tone(900, 50, 0.15)
                _time_mod.sleep(0.06)
                self.play_tone(700, 50, 0.15)
                _time_mod.sleep(0.06)
                self.play_tone(500, 70, 0.15)
            elif style == "heartbeat":
                # Gentle double-tap — ambient/status pulse
                self.play_tone(400, 30, 0.1)
                _time_mod.sleep(0.15)
                self.play_tone(400, 40, 0.12)
            elif style == "inbox":
                # Distinct from "choices": quick triple ascending notes
                self.play_tone(500, 40, 0.12)
                _time_mod.sleep(0.05)
                self.play_tone(700, 40, 0.12)
                _time_mod.sleep(0.05)
                self.play_tone(1000, 60, 0.18)

        threading.Thread(target=_play, daemon=True).start()
