"""TTS engine with two backends: local espeak-ng and API tts tool.

Supports pregeneration: generate audio files for a batch of texts in
parallel, then play them instantly on demand from cache.

The tts tool is configured via IoMcpConfig (config.yml) which passes
explicit CLI flags for provider, model, voice, speed, base-url, api-key.
"""

from __future__ import annotations

import hashlib
import os
import signal
import shutil
import subprocess
import tempfile
import threading
import time as _time_mod
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from .config import IoMcpConfig


# espeak-ng words per minute
TTS_SPEED = 160

# Path to the gpt-4o-mini-tts wrapper
TTS_TOOL_DIR = os.path.expanduser("~/mono/tools/tts")

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
        self._process: Optional[subprocess.Popen] = None
        self._streaming_tts_proc: Optional[subprocess.Popen] = None
        self._termux_proc: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()
        self._local = local
        self._speed = speed
        self._muted = False  # when True, play_cached is a no-op
        self._config = config

        # PulseAudio/paplay health tracking
        self._consecutive_failures = 0
        self._last_failure_time: float = 0
        self._last_failure_msg: str = ""
        self._total_failures = 0
        self._total_plays = 0
        self._max_retries = 2  # retry paplay this many times on failure

        self._env = os.environ.copy()
        self._env["PULSE_SERVER"] = os.environ.get("PULSE_SERVER", "127.0.0.1")
        self._env["LD_LIBRARY_PATH"] = PORTAUDIO_LIB

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
            tts_main = os.path.join(TTS_TOOL_DIR, "src", "tts", "__main__.py")
            if not os.path.isfile(tts_main):
                print("WARNING: tts tool not found — falling back to espeak-ng", flush=True)
                self._local = True

        # Audio cache: text hash → file path
        self._cache: dict[str, str] = {}
        os.makedirs(CACHE_DIR, exist_ok=True)

        mode = "espeak-ng (local)" if self._local else "tts CLI (API)"
        if not self._local and self._config:
            mode = f"{self._config.tts_model_name} ({self._config.tts_voice})"
        local_mode = {"termux": "termux-tts-speak", "espeak": "espeak-ng", "none": "none"}[self._local_backend]
        print(f"  TTS engine: {mode}", flush=True)
        print(f"  TTS local: {local_mode}", flush=True)

    def _cache_key(self, text: str, voice_override: Optional[str] = None,
                   emotion_override: Optional[str] = None) -> str:
        # Include backend, speed, and config-based settings in cache key
        # so cache is invalidated when voice/model/speed changes
        if self._config and not self._local:
            voice = voice_override or self._config.tts_voice
            emotion = emotion_override or self._config.tts_emotion
            params = (
                f"{text}|local={self._local}"
                f"|model={self._config.tts_model_name}"
                f"|voice={voice}"
                f"|speed={self._config.tts_speed}"
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
        try:
            with open("/tmp/io-mcp-tui-error.log", "a") as f:
                f.write(
                    f"\n--- TTS playback failure "
                    f"(consecutive: {self._consecutive_failures}, "
                    f"total: {self._total_failures}) ---\n"
                    f"{message}\n"
                    f"PULSE_SERVER={self._env.get('PULSE_SERVER', 'unset')}\n"
                )
        except Exception:
            pass

    def _log_recovery(self, attempt: int) -> None:
        """Log recovery from playback failures."""
        try:
            with open("/tmp/io-mcp-tui-error.log", "a") as f:
                f.write(
                    f"\n--- TTS recovered after {self._consecutive_failures} "
                    f"failure(s), retry attempt {attempt} ---\n"
                )
        except Exception:
            pass

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
                          emotion_override: Optional[str] = None) -> Optional[str]:
        """Generate audio for text and save to a WAV file. Returns file path."""
        key = self._cache_key(text, voice_override, emotion_override)

        # Check cache
        cached = self._cache.get(key)
        if cached and os.path.isfile(cached):
            return cached

        out_path = os.path.join(CACHE_DIR, f"{key}.wav")

        try:
            if self._local:
                if not self._espeak:
                    return None
                # espeak-ng outputs WAV directly; scale WPM by speed
                wpm = int(TTS_SPEED * self._speed)
                cmd = [self._espeak, "--stdout", "-s", str(wpm), text]
                with open(out_path, "wb") as f:
                    proc = subprocess.run(
                        cmd, stdout=f, stderr=subprocess.DEVNULL,
                        env=self._env, timeout=10,
                    )
                if proc.returncode != 0:
                    return None
            else:
                if not self._tts_bin:
                    return None
                # Build tts command from config (explicit flags)
                if self._config:
                    cmd = [self._tts_bin] + self._config.tts_cli_args(
                        text, voice_override=voice_override,
                        emotion_override=emotion_override)
                else:
                    # Legacy fallback: no config, use env vars
                    cmd = [self._tts_bin, text, "--stdout", "--response-format", "wav"]

                with open(out_path, "wb") as f:
                    proc = subprocess.run(
                        cmd, stdout=f, stderr=subprocess.DEVNULL,
                        env=self._env, timeout=30,
                    )
                if proc.returncode != 0:
                    try:
                        os.unlink(out_path)
                    except OSError:
                        pass
                    return None

            self._cache[key] = out_path
            return out_path

        except Exception:
            return None

    def pregenerate(self, texts: list[str]) -> None:
        """Generate audio clips for all texts in parallel.

        Call this when choices arrive so scrolling is instant.
        """
        # Filter out already-cached texts
        to_generate = [t for t in texts if self._cache_key(t) not in self._cache]
        if not to_generate:
            return

        # Generate in parallel (up to 4 concurrent)
        max_workers = min(4, len(to_generate))
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            pool.map(self._generate_to_file, to_generate)

    def play_cached(self, text: str, block: bool = False,
                    voice_override: Optional[str] = None,
                    emotion_override: Optional[str] = None) -> None:
        """Play a pregenerated audio clip. Falls back to live generation.

        If block=True, waits for playback to finish before returning.
        If block=False, starts playback and returns immediately.
        """
        if not self._paplay or self._muted:
            return

        key = self._cache_key(text, voice_override, emotion_override)
        path = self._cache.get(key)

        if path and os.path.isfile(path):
            # Fast path: cached — kill current and play immediately
            self.stop()
            _time_mod.sleep(0.05)  # Brief pause to let PulseAudio settle
            self._start_playback(path)
            if block:
                self._wait_for_playback()
        else:
            # Slow path: generate on demand
            p = self._generate_to_file(text, voice_override, emotion_override)
            if p:
                self.stop()
                _time_mod.sleep(0.05)  # Brief pause to let PulseAudio settle
                self._start_playback(p)
                if block:
                    self._wait_for_playback()

    def _start_playback(self, path: str) -> bool:
        """Start paplay for a WAV file. Returns True if playback started ok.

        Detects immediate paplay failures (e.g. PulseAudio connection refused)
        and retries up to _max_retries times. Logs failures for diagnostics.
        """
        for attempt in range(1 + self._max_retries):
            with self._lock:
                try:
                    self._process = subprocess.Popen(
                        [self._paplay, path],
                        env=self._env,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.PIPE,
                        preexec_fn=os.setsid,
                    )
                except Exception as e:
                    self._process = None
                    self._record_failure(f"Failed to start paplay: {e}")
                    continue

            # Give paplay a moment to fail on connection errors
            proc = self._process
            if proc is None:
                continue
            try:
                retcode = proc.wait(timeout=0.3)
                # Process exited immediately — likely a connection error
                stderr_out = ""
                try:
                    stderr_out = (proc.stderr.read() or b"").decode("utf-8", errors="replace").strip()
                except Exception:
                    pass
                if retcode != 0:
                    self._record_failure(
                        f"paplay exited immediately (code {retcode}): {stderr_out or 'no stderr'}"
                    )
                    with self._lock:
                        self._process = None
                    # Brief pause before retry to let PulseAudio settle
                    if attempt < self._max_retries:
                        _time_mod.sleep(0.2 * (attempt + 1))
                    continue
            except subprocess.TimeoutExpired:
                # Still running after 0.3s — playback started successfully
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
        proc = self._process
        if proc is not None:
            try:
                retcode = proc.wait(timeout=30)
                if retcode != 0:
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
              emotion_override: Optional[str] = None) -> None:
        """Speak text and BLOCK until playback finishes.

        When in local mode, uses the configured local backend (termux-tts-speak
        or espeak-ng) rather than always falling back to espeak file generation.
        """
        if self._local and self._local_backend == "termux" and self._termux_exec:
            self._speak_termux(text)
            return
        self.play_cached(text, block=True, voice_override=voice_override,
                        emotion_override=emotion_override)

    def speak_async(self, text: str, voice_override: Optional[str] = None,
                    emotion_override: Optional[str] = None) -> None:
        """Speak text without blocking. Used for scroll TTS.

        When in local mode, uses the configured local backend (termux-tts-speak
        or espeak-ng) rather than always falling back to espeak file generation.
        """
        def _do():
            try:
                if self._local and self._local_backend == "termux" and self._termux_exec:
                    self._speak_termux(text)
                    return
                self.play_cached(text, block=False, voice_override=voice_override,
                               emotion_override=emotion_override)
            except Exception as e:
                try:
                    with open("/tmp/io-mcp-tui-error.log", "a") as f:
                        import traceback
                        f.write(f"\n--- speak_async error ---\n{traceback.format_exc()}\n")
                except Exception:
                    pass
        threading.Thread(target=_do, daemon=True).start()

    def is_cached(self, text: str, voice_override: Optional[str] = None,
                  emotion_override: Optional[str] = None) -> bool:
        """Check if audio for this text is already generated."""
        key = self._cache_key(text, voice_override, emotion_override)
        path = self._cache.get(key)
        return path is not None and os.path.isfile(path)

    def _speak_termux(self, text: str) -> None:
        """Speak text via termux-tts-speak (Android native TTS).

        Uses the MUSIC audio stream for nice output quality.
        Blocks until speech finishes. No files, no PulseAudio needed.
        """
        if not self._termux_exec:
            return

        # Kill any previous termux-tts-speak
        with self._lock:
            if self._termux_proc and self._termux_proc.poll() is None:
                try:
                    self._termux_proc.kill()
                except (OSError, ProcessLookupError):
                    pass
                self._termux_proc = None

        try:
            speed = self._config.tts_speed if self._config else self._speed
            cmd = [self._termux_exec, "termux-tts-speak",
                   "-s", "MUSIC", "-r", str(speed), text]
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            with self._lock:
                self._termux_proc = proc
            proc.wait(timeout=30)
        except Exception:
            pass

    def speak_with_local_fallback(self, text: str,
                                   voice_override: Optional[str] = None,
                                   emotion_override: Optional[str] = None) -> None:
        """Speak text instantly: use cache if available, else local fallback.

        For scroll-through option readout where latency matters more than
        voice quality. If the API TTS is cached, plays the nice version.
        If not, uses the configured local backend (termux-tts-speak or espeak)
        immediately and kicks off background generation so the next visit
        to this option will use the full API TTS.
        """
        if self._muted:
            return

        key = self._cache_key(text, voice_override, emotion_override)
        path = self._cache.get(key)

        if path and os.path.isfile(path):
            # Cache hit — play the full quality version
            self.stop()
            _time_mod.sleep(0.05)
            self._start_playback(path)
            return

        # Cache miss — use configured local backend for instant readout
        if self._local_backend == "termux" and self._termux_exec:
            # termux-tts-speak: direct Android TTS, no PulseAudio needed
            def _termux_play():
                self._speak_termux(text)
            threading.Thread(target=_termux_play, daemon=True).start()

            # Also kick off background API generation for next time
            if not self._local:
                def _gen():
                    self._generate_to_file(text, voice_override, emotion_override)
                threading.Thread(target=_gen, daemon=True).start()

        elif self._local_backend == "espeak" and self._espeak and self._paplay:
            # espeak-ng: generate WAV and play via paplay
            def _espeak_play():
                try:
                    wpm = int(TTS_SPEED * self._speed)
                    # Generate espeak audio to a temp file and play it
                    tmp = os.path.join(CACHE_DIR, f"_espeak_{hashlib.md5(text.encode()).hexdigest()[:8]}.wav")
                    with open(tmp, "wb") as f:
                        subprocess.run(
                            [self._espeak, "--stdout", "-s", str(wpm), text],
                            stdout=f, stderr=subprocess.DEVNULL,
                            env=self._env, timeout=5,
                        )
                    self.stop()
                    _time_mod.sleep(0.05)
                    self._start_playback(tmp)
                except Exception:
                    pass
            threading.Thread(target=_espeak_play, daemon=True).start()

            # Also kick off background API generation for next time
            if not self._local:
                def _gen():
                    self._generate_to_file(text, voice_override, emotion_override)
                threading.Thread(target=_gen, daemon=True).start()
        else:
            # No local backend — fall back to normal async (will generate and play)
            self.speak_async(text, voice_override, emotion_override)

    def speak_streaming(self, text: str, voice_override: Optional[str] = None,
                        emotion_override: Optional[str] = None,
                        block: bool = True) -> None:
        """Speak text by piping tts stdout directly to paplay (no file).

        This reduces time-to-first-audio because playback starts as soon
        as the TTS service sends initial WAV data, rather than waiting for
        the entire response. Falls back to cached play if streaming is
        unavailable (local mode or missing binaries).

        Args:
            text: Text to speak.
            voice_override: Optional voice name for per-session rotation.
            emotion_override: Optional emotion preset for per-session rotation.
            block: If True, wait for playback to finish before returning.
        """
        if not self._paplay or self._muted:
            return

        # Check cache first — if we have a cached file, no need to stream
        key = self._cache_key(text, voice_override, emotion_override)
        cached = self._cache.get(key)
        if cached and os.path.isfile(cached):
            self.stop()
            self._start_playback(cached)
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
                           emotion_override=emotion_override)
            return

        # Build tts command
        cmd = [self._tts_bin] + self._config.tts_cli_args(
            text, voice_override=voice_override,
            emotion_override=emotion_override)

        self.stop()

        try:
            with self._lock:
                # Pipe tts stdout directly to paplay — audio starts immediately
                # WAV header is in the stream, so paplay can decode it directly
                tts_proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    env=self._env,
                    preexec_fn=os.setsid,
                )
                play_proc = subprocess.Popen(
                    [self._paplay],
                    stdin=tts_proc.stdout,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    env=self._env,
                    preexec_fn=os.setsid,
                )
                # Allow tts_proc to receive SIGPIPE if paplay exits
                if tts_proc.stdout:
                    tts_proc.stdout.close()
                self._process = play_proc
                self._streaming_tts_proc = tts_proc

            if block:
                try:
                    retcode = play_proc.wait(timeout=60)
                    if retcode != 0:
                        stderr_out = ""
                        try:
                            stderr_out = (play_proc.stderr.read() or b"").decode("utf-8", errors="replace").strip()
                        except Exception:
                            pass
                        self._record_failure(
                            f"paplay (streaming) exited with code {retcode}: {stderr_out or 'no stderr'}"
                        )
                    else:
                        self._total_plays += 1
                        self._consecutive_failures = 0
                except subprocess.TimeoutExpired:
                    self._record_failure("paplay (streaming) timed out after 60s")
                except Exception:
                    pass
                try:
                    tts_proc.wait(timeout=5)
                except Exception:
                    pass
        except Exception as e:
            self._record_failure(f"Streaming TTS setup failed: {e}")
            # Fall back to non-streaming on any error
            self.play_cached(text, block=block, voice_override=voice_override,
                           emotion_override=emotion_override)

    def speak_streaming_async(self, text: str, voice_override: Optional[str] = None,
                              emotion_override: Optional[str] = None) -> None:
        """Speak text via streaming pipe without blocking caller."""
        def _do():
            self.speak_streaming(text, voice_override=voice_override,
                               emotion_override=emotion_override, block=False)
        threading.Thread(target=_do, daemon=True).start()

    def stop(self) -> None:
        """Kill any in-progress playback and streaming TTS (non-blocking)."""
        with self._lock:
            for proc in (self._process, self._streaming_tts_proc):
                if proc and proc.poll() is None:
                    try:
                        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                    except (OSError, ProcessLookupError):
                        pass
            self._process = None
            self._streaming_tts_proc = None
            # Also kill termux-tts-speak (doesn't use process groups)
            if self._termux_proc and self._termux_proc.poll() is None:
                try:
                    self._termux_proc.kill()
                except (OSError, ProcessLookupError):
                    pass
            self._termux_proc = None

    def mute(self) -> None:
        """Stop playback and prevent any new audio from playing."""
        self._muted = True
        self.stop()

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
        """Attempt to reconnect PulseAudio with escalating recovery strategies.

        Tries several strategies in order:
        1. pactl info (just check if it's already back)
        2. Kill orphaned paplay processes that may be holding resources
        3. pulseaudio --start (restart daemon if needed)
        4. Suspend-unsuspend sinks to force reconnect
        5. Kill and restart PulseAudio daemon entirely

        Returns (success, diagnostic_info) tuple:
            success: True if PulseAudio is reachable after reconnection attempts.
            diagnostic_info: String with diagnostic details for logging/notifications.
        """
        env = self._env.copy()
        pactl = _find_binary("pactl")
        pulseaudio = _find_binary("pulseaudio")
        pulse_server = env.get("PULSE_SERVER", "127.0.0.1")
        diagnostics: list[str] = []

        # Strategy 1: Check if PulseAudio is already back
        if pactl:
            try:
                result = subprocess.run(
                    [pactl, "info"],
                    env=env, capture_output=True, timeout=3,
                )
                if result.returncode == 0:
                    return True, "PulseAudio was already reachable"
                diagnostics.append(f"pactl info failed (rc={result.returncode})")
            except subprocess.TimeoutExpired:
                diagnostics.append("pactl info timed out")
            except Exception as e:
                diagnostics.append(f"pactl info error: {e}")

        # Strategy 2: Kill orphaned paplay processes that may be holding resources
        try:
            result = subprocess.run(
                ["pkill", "-f", "paplay"],
                capture_output=True, timeout=2,
            )
            if result.returncode == 0:
                diagnostics.append("killed orphaned paplay processes")
                _time_mod.sleep(0.3)
            else:
                diagnostics.append("no orphaned paplay processes found")
        except Exception:
            pass

        # Strategy 3: Try to start PulseAudio daemon
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

        # Check after daemon start
        if pactl:
            try:
                result = subprocess.run(
                    [pactl, "info"],
                    env=env, capture_output=True, timeout=3,
                )
                if result.returncode == 0:
                    return True, "; ".join(diagnostics + ["recovered after daemon start"])
            except Exception:
                pass

        # Strategy 4: Try reconnecting by suspending and resuming sinks
        if pactl:
            try:
                subprocess.run(
                    [pactl, "suspend-sink", "", "1"],
                    env=env, capture_output=True, timeout=2,
                )
                _time_mod.sleep(0.2)
                subprocess.run(
                    [pactl, "suspend-sink", "", "0"],
                    env=env, capture_output=True, timeout=2,
                )
                diagnostics.append("sink suspend/resume attempted")
            except Exception:
                pass

        # Strategy 5: Kill PulseAudio entirely and restart
        if pulseaudio:
            try:
                subprocess.run(
                    [pulseaudio, "--kill"],
                    env=env, capture_output=True, timeout=3,
                )
                _time_mod.sleep(0.5)
                result = subprocess.run(
                    [pulseaudio, "--start"],
                    env=env, capture_output=True, timeout=5,
                )
                if result.returncode == 0:
                    diagnostics.append("pulseaudio kill+restart succeeded")
                else:
                    diagnostics.append("pulseaudio kill+restart failed")
            except Exception as e:
                diagnostics.append(f"pulseaudio kill+restart error: {e}")

        # Final check: is PulseAudio back?
        if pactl:
            try:
                result = subprocess.run(
                    [pactl, "info"],
                    env=env, capture_output=True, timeout=3,
                )
                if result.returncode == 0:
                    return True, "; ".join(diagnostics + ["recovered after full restart"])
                # Capture server info for diagnostics
                stderr = result.stderr.decode("utf-8", errors="replace").strip()
                if stderr:
                    diagnostics.append(f"final check stderr: {stderr[:100]}")
            except Exception:
                pass

        # All strategies failed — add PULSE_SERVER info for diagnostics
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
        self.stop()
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
