"""TTS engine with two backends: local espeak-ng and API gpt-4o-mini-tts."""

from __future__ import annotations

import os
import signal
import shutil
import subprocess
import threading
from typing import Optional


# espeak-ng words per minute
TTS_SPEED = 160

# Path to the gpt-4o-mini-tts wrapper
TTS_TOOL_DIR = os.path.expanduser("~/mono/tools/tts")

# LD_LIBRARY_PATH needed for sounddevice/portaudio on NOD
PORTAUDIO_LIB = "/nix/store/7r2nbdnd4f0mpwkkknix2sl3zm67nlkf-nix-on-droid-path/lib"


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
    """Text-to-speech with two backends:

    - local: espeak-ng --stdout | paplay  (fast, robotic)
    - api:   tts --stdout | paplay  (gpt-4o-mini-tts via OpenAI API, nice voice)
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
            # Check if tts tool source exists for fallback
            tts_main = os.path.join(TTS_TOOL_DIR, "src", "tts", "__main__.py")
            if not os.path.isfile(tts_main):
                print("WARNING: tts tool not found — falling back to espeak-ng", flush=True)
                self._local = True

        mode = "espeak-ng (local)" if self._local else "gpt-4o-mini-tts (API)"
        print(f"  TTS: {mode}", flush=True)

    def speak(self, text: str) -> None:
        """Speak text asynchronously, cancelling any in-progress speech."""
        if not self._paplay:
            return
        if self._local and not self._espeak:
            return
        self.stop()
        clean = text.replace("'", "'\\''")
        with self._lock:
            try:
                if self._local:
                    cmd = f"{self._espeak} --stdout -s {TTS_SPEED} '{clean}' | {self._paplay}"
                else:
                    if self._tts_bin:
                        cmd = (
                            f"{self._tts_bin} '{clean}' --stdout 2>/dev/null"
                            f" | {self._paplay} --raw --format=s16le --rate=24000 --channels=1"
                        )
                    else:
                        cmd = (
                            f"python3 -m tts '{clean}' --stdout 2>/dev/null"
                            f" | {self._paplay} --raw --format=s16le --rate=24000 --channels=1"
                        )
                self._process = subprocess.Popen(
                    cmd,
                    shell=True,
                    env=self._env,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    preexec_fn=os.setsid,
                )
            except Exception:
                self._process = None

    def stop(self) -> None:
        """Kill any in-progress TTS."""
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

    def cleanup(self) -> None:
        self.stop()
