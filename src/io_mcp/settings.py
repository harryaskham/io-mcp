"""Settings state for the io-mcp TUI.

Backed by IoMcpConfig — reads/writes config.yml on changes.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from .config import IoMcpConfig


class Settings:
    """Runtime settings managed via the in-TUI settings menu.

    Backed by IoMcpConfig — reads/writes config.yml on changes.
    """

    def __init__(self, config: Optional["IoMcpConfig"] = None):
        self._config = config
        self._pre_fast_speed: float | None = None  # for fast toggle

    @property
    def speed(self) -> float:
        if self._config:
            return self._config.tts_speed
        return float(os.environ.get("TTS_SPEED", "1.0"))

    @speed.setter
    def speed(self, value: float) -> None:
        if self._config:
            self._config.set_tts_speed(value)
            self._config.save()

    @property
    def voice(self) -> str:
        """Current voice preset name."""
        if self._config:
            return self._config.tts_voice_preset
        return os.environ.get("OPENAI_TTS_VOICE", "sage")

    @voice.setter
    def voice(self, value: str) -> None:
        """Set voice by preset name."""
        if self._config:
            self._config.set_tts_voice(value)
            self._config.save()

    @property
    def tts_model(self) -> str:
        if self._config:
            return self._config.tts_model_name
        return "gpt-4o-mini-tts"

    @tts_model.setter
    def tts_model(self, value: str) -> None:
        if self._config:
            self._config.set_tts_model(value)
            self._config.save()

    @property
    def stt_model(self) -> str:
        if self._config:
            return self._config.stt_model_name
        return "whisper"

    @stt_model.setter
    def stt_model(self, value: str) -> None:
        if self._config:
            self._config.set_stt_model(value)
            self._config.save()

    @property
    def emotion(self) -> str:
        if self._config:
            return self._config.tts_emotion
        return "neutral"

    @emotion.setter
    def emotion(self, value: str) -> None:
        if self._config:
            self._config.set_tts_emotion(value)
            self._config.save()

    def get_emotions(self) -> list[str]:
        if self._config:
            return self._config.emotion_preset_names
        return ["neutral"]

    def get_voices(self) -> list[str]:
        """Get available voice preset names."""
        if self._config:
            return self._config.voice_preset_names
        return ["sage", "ballad", "alloy"]

    def get_tts_models(self) -> list[str]:
        if self._config:
            return self._config.tts_model_names
        return ["gpt-4o-mini-tts"]

    def get_stt_models(self) -> list[str]:
        if self._config:
            return self._config.stt_model_names
        return ["whisper"]

    def get_voice_model_pairs(self) -> list[tuple[str, str]]:
        """Get all voice preset name + model combinations.

        Returns list of (preset_name, model_name) tuples.
        E.g. [("sage", "gpt-4o-mini-tts"), ("noa", "mai-voice-1"), ...]
        """
        if not self._config:
            return [("sage", "gpt-4o-mini-tts")]
        pairs = []
        for name in self._config.voice_preset_names:
            resolved = self._config.resolve_voice(name)
            pairs.append((name, resolved["model"]))
        return pairs

    def set_voice_and_model(self, voice: str, model: str) -> None:
        """Set voice by preset name (model is inferred from preset)."""
        if self._config:
            self._config.set_tts_voice(voice)
            self._config.save()

    def apply_to_env(self):
        """Push current settings to env vars (legacy compat)."""
        os.environ["TTS_SPEED"] = str(self.speed)

    def toggle_fast(self) -> str:
        if self._pre_fast_speed is not None:
            self.speed = self._pre_fast_speed
            self._pre_fast_speed = None
            msg = f"Speed reset to {self.speed}"
        else:
            self._pre_fast_speed = self.speed
            self.speed = 1.8
            msg = "Speed set to 1.8"
        return msg

    def toggle_voice(self) -> str:
        voices = self.get_voices()
        if not voices:
            return "No voices available"
        current = self.voice
        if current in voices:
            idx = voices.index(current)
            self.voice = voices[(idx + 1) % len(voices)]
        else:
            self.voice = voices[0]
        return f"Voice: {self.voice}"
