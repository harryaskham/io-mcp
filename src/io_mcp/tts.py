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
    """Text-to-speech with two backends and pregeneration support.

    - local: espeak-ng (fast, robotic)
    - api:   tts CLI tool (nice voice, slower) — configured via IoMcpConfig

    Audio is generated to WAV files, then played via paplay.
    pregenerate() creates clips in parallel so scrolling is instant.
    """

    def __init__(self, local: bool = False, speed: float = 1.0,
                 config: Optional["IoMcpConfig"] = None):
        self._process: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()
        self._local = local
        self._speed = speed
        self._muted = False  # when True, play_cached is a no-op
        self._config = config

        self._env = os.environ.copy()
        self._env["PULSE_SERVER"] = os.environ.get("PULSE_SERVER", "127.0.0.1")
        self._env["LD_LIBRARY_PATH"] = PORTAUDIO_LIB

        self._paplay = _find_binary("paplay")
        self._espeak = _find_binary("espeak-ng")
        self._tts_bin = _find_binary("tts")

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
        print(f"  TTS engine: {mode}", flush=True)

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
            self._start_playback(path)
            if block:
                self._wait_for_playback()
        else:
            # Slow path: generate on demand
            p = self._generate_to_file(text, voice_override, emotion_override)
            if p:
                self.stop()
                self._start_playback(p)
                if block:
                    self._wait_for_playback()

    def _start_playback(self, path: str) -> None:
        """Start paplay for a WAV file."""
        with self._lock:
            try:
                self._process = subprocess.Popen(
                    [self._paplay, path],
                    env=self._env,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    preexec_fn=os.setsid,
                )
            except Exception:
                self._process = None

    def _wait_for_playback(self) -> None:
        """Wait for current playback to finish."""
        proc = self._process
        if proc is not None:
            try:
                proc.wait(timeout=30)
            except Exception:
                pass

    def speak(self, text: str, voice_override: Optional[str] = None,
              emotion_override: Optional[str] = None) -> None:
        """Speak text and BLOCK until playback finishes."""
        self.play_cached(text, block=True, voice_override=voice_override,
                        emotion_override=emotion_override)

    def speak_async(self, text: str, voice_override: Optional[str] = None,
                    emotion_override: Optional[str] = None) -> None:
        """Speak text without blocking. Used for scroll TTS."""
        def _do():
            self.play_cached(text, block=False, voice_override=voice_override,
                           emotion_override=emotion_override)
        threading.Thread(target=_do, daemon=True).start()

    def stop(self) -> None:
        """Kill any in-progress playback (non-blocking)."""
        with self._lock:
            if self._process and self._process.poll() is None:
                try:
                    os.killpg(os.getpgid(self._process.pid), signal.SIGKILL)
                except (OSError, ProcessLookupError):
                    pass
                self._process = None

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

    def cleanup(self) -> None:
        self.stop()
        self.clear_cache()
