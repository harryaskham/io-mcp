"""TTS engine with two backends: local espeak-ng and API gpt-4o-mini-tts.

Supports pregeneration: generate audio files for a batch of texts in
parallel, then play them instantly on demand from cache.
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
from typing import Optional


# espeak-ng words per minute
TTS_SPEED = 160

# Path to the gpt-4o-mini-tts wrapper
TTS_TOOL_DIR = os.path.expanduser("~/mono/tools/tts")

# LD_LIBRARY_PATH needed for sounddevice/portaudio on NOD
PORTAUDIO_LIB = "/nix/store/7r2nbdnd4f0mpwkkknix2sl3zm67nlkf-nix-on-droid-path/lib"

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
    - api:   tts CLI tool → gpt-4o-mini-tts (nice voice, slower)

    Audio is generated to WAV files, then played via paplay.
    pregenerate() creates clips in parallel so scrolling is instant.
    """

    def __init__(self, local: bool = False):
        self._process: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()
        self._local = local

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

        mode = "espeak-ng (local)" if self._local else "gpt-4o-mini-tts (API)"
        print(f"  TTS: {mode}", flush=True)

    def _cache_key(self, text: str) -> str:
        return hashlib.md5(text.encode()).hexdigest()

    def _generate_to_file(self, text: str) -> Optional[str]:
        """Generate audio for text and save to a WAV file. Returns file path."""
        key = self._cache_key(text)

        # Check cache
        cached = self._cache.get(key)
        if cached and os.path.isfile(cached):
            return cached

        clean = text.replace("'", "'\\''")
        out_path = os.path.join(CACHE_DIR, f"{key}.wav")

        try:
            if self._local:
                if not self._espeak:
                    return None
                # espeak-ng outputs WAV directly
                cmd = [self._espeak, "--stdout", "-s", str(TTS_SPEED), text]
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
                # tts tool: request WAV (self-describing, paplay auto-detects)
                out_path = os.path.join(CACHE_DIR, f"{key}.wav")
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

    def play_cached(self, text: str) -> None:
        """Play a pregenerated audio clip. Falls back to live generation."""
        if not self._paplay:
            return
        self.stop()

        key = self._cache_key(text)
        path = self._cache.get(key)

        if not path or not os.path.isfile(path):
            # Generate on demand (fallback)
            path = self._generate_to_file(text)

        if not path:
            return

        with self._lock:
            try:
                # All cached files are WAV — paplay auto-detects format
                cmd = [self._paplay, path]

                self._process = subprocess.Popen(
                    cmd,
                    env=self._env,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    preexec_fn=os.setsid,
                )
            except Exception:
                self._process = None

    def speak(self, text: str) -> None:
        """Speak text — uses cache if available, otherwise generates live."""
        self.play_cached(text)

    def stop(self) -> None:
        """Kill any in-progress playback."""
        with self._lock:
            if self._process and self._process.poll() is None:
                try:
                    os.killpg(os.getpgid(self._process.pid), signal.SIGKILL)
                except (OSError, ProcessLookupError):
                    pass
                try:
                    self._process.wait(timeout=0.5)
                except Exception:
                    pass
                self._process = None

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
