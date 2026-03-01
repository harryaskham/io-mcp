"""Tests for the io-mcp Settings module.

Settings wraps IoMcpConfig with property accessors used by the TUI
settings menu. Tests use mock config objects to isolate from config.py.
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, call

import pytest

from io_mcp.settings import Settings


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_mock_config(
    *,
    tts_speed: float = 1.0,
    tts_voice_preset: str = "sage",
    tts_model_name: str = "gpt-4o-mini-tts",
    stt_model_name: str = "whisper",
    tts_emotion: str = "neutral",
    voice_preset_names: list[str] | None = None,
    emotion_preset_names: list[str] | None = None,
    tts_model_names: list[str] | None = None,
    stt_model_names: list[str] | None = None,
    resolve_voice_map: dict[str, dict] | None = None,
) -> MagicMock:
    """Create a mock IoMcpConfig with sensible defaults."""
    cfg = MagicMock()
    cfg.tts_speed = tts_speed
    cfg.tts_voice_preset = tts_voice_preset
    cfg.tts_model_name = tts_model_name
    cfg.stt_model_name = stt_model_name
    cfg.tts_emotion = tts_emotion

    cfg.voice_preset_names = voice_preset_names if voice_preset_names is not None else ["sage", "noa", "teo"]
    cfg.emotion_preset_names = emotion_preset_names if emotion_preset_names is not None else [
        "neutral", "happy", "excited", "whispering",
    ]
    cfg.tts_model_names = tts_model_names if tts_model_names is not None else [
        "gpt-4o-mini-tts", "azure/speech/azure-tts",
    ]
    cfg.stt_model_names = stt_model_names if stt_model_names is not None else ["whisper", "mai-ears-1"]

    # resolve_voice returns a dict with provider/model/voice
    default_resolve = {
        "sage": {"provider": "openai", "model": "gpt-4o-mini-tts", "voice": "sage"},
        "noa": {"provider": "openai", "model": "azure/speech/azure-tts", "voice": "en-US-Noa:MAI-Voice-1"},
        "teo": {"provider": "openai", "model": "azure/speech/azure-tts", "voice": "en-US-Teo:MAI-Voice-1"},
    }
    mapping = resolve_voice_map if resolve_voice_map is not None else default_resolve
    cfg.resolve_voice.side_effect = lambda name: mapping.get(
        name, {"provider": "openai", "model": "gpt-4o-mini-tts", "voice": name}
    )

    return cfg


# ---------------------------------------------------------------------------
# Property getters — with config
# ---------------------------------------------------------------------------

class TestPropertyGettersWithConfig:
    """Property getters delegate to the config object."""

    def test_speed(self):
        cfg = make_mock_config(tts_speed=1.5)
        s = Settings(cfg)
        assert s.speed == 1.5

    def test_voice(self):
        cfg = make_mock_config(tts_voice_preset="noa")
        s = Settings(cfg)
        assert s.voice == "noa"

    def test_tts_model(self):
        cfg = make_mock_config(tts_model_name="azure/speech/azure-tts")
        s = Settings(cfg)
        assert s.tts_model == "azure/speech/azure-tts"

    def test_stt_model(self):
        cfg = make_mock_config(stt_model_name="mai-ears-1")
        s = Settings(cfg)
        assert s.stt_model == "mai-ears-1"

    def test_emotion(self):
        cfg = make_mock_config(tts_emotion="excited")
        s = Settings(cfg)
        assert s.emotion == "excited"


# ---------------------------------------------------------------------------
# Property getters — without config (env fallback)
# ---------------------------------------------------------------------------

class TestPropertyGettersWithoutConfig:
    """When config is None, properties fall back to env vars / defaults."""

    def test_speed_default(self, monkeypatch):
        monkeypatch.delenv("TTS_SPEED", raising=False)
        s = Settings(None)
        assert s.speed == 1.0

    def test_speed_from_env(self, monkeypatch):
        monkeypatch.setenv("TTS_SPEED", "2.0")
        s = Settings(None)
        assert s.speed == 2.0

    def test_voice_default(self, monkeypatch):
        monkeypatch.delenv("OPENAI_TTS_VOICE", raising=False)
        s = Settings(None)
        assert s.voice == "sage"

    def test_voice_from_env(self, monkeypatch):
        monkeypatch.setenv("OPENAI_TTS_VOICE", "nova")
        s = Settings(None)
        assert s.voice == "nova"

    def test_tts_model_default(self):
        s = Settings(None)
        assert s.tts_model == "gpt-4o-mini-tts"

    def test_stt_model_default(self):
        s = Settings(None)
        assert s.stt_model == "whisper"

    def test_emotion_default(self):
        s = Settings(None)
        assert s.emotion == "neutral"


# ---------------------------------------------------------------------------
# Property setters
# ---------------------------------------------------------------------------

class TestPropertySetters:
    """Setters call the appropriate config mutation + save."""

    def test_speed_setter(self):
        cfg = make_mock_config()
        s = Settings(cfg)
        s.speed = 2.5
        cfg.set_tts_speed.assert_called_once_with(2.5)
        cfg.save.assert_called_once()

    def test_voice_setter(self):
        cfg = make_mock_config()
        s = Settings(cfg)
        s.voice = "coral"
        cfg.set_tts_voice.assert_called_once_with("coral")
        cfg.save.assert_called_once()

    def test_tts_model_setter(self):
        cfg = make_mock_config()
        s = Settings(cfg)
        s.tts_model = "azure/speech/azure-tts"
        cfg.set_tts_model.assert_called_once_with("azure/speech/azure-tts")
        cfg.save.assert_called_once()

    def test_stt_model_setter(self):
        cfg = make_mock_config()
        s = Settings(cfg)
        s.stt_model = "mai-ears-1"
        cfg.set_stt_model.assert_called_once_with("mai-ears-1")
        cfg.save.assert_called_once()

    def test_emotion_setter(self):
        cfg = make_mock_config()
        s = Settings(cfg)
        s.emotion = "happy"
        cfg.set_tts_emotion.assert_called_once_with("happy")
        cfg.save.assert_called_once()


class TestPropertySettersWithoutConfig:
    """Setters are no-ops when config is None (no crash)."""

    def test_speed_setter_no_config(self):
        s = Settings(None)
        s.speed = 2.0  # should not raise

    def test_voice_setter_no_config(self):
        s = Settings(None)
        s.voice = "alloy"

    def test_tts_model_setter_no_config(self):
        s = Settings(None)
        s.tts_model = "gpt-4o-mini-tts"

    def test_stt_model_setter_no_config(self):
        s = Settings(None)
        s.stt_model = "whisper"

    def test_emotion_setter_no_config(self):
        s = Settings(None)
        s.emotion = "calm"


# ---------------------------------------------------------------------------
# get_emotions()
# ---------------------------------------------------------------------------

class TestGetEmotions:
    def test_returns_preset_names_from_config(self):
        cfg = make_mock_config(emotion_preset_names=["happy", "calm", "excited"])
        s = Settings(cfg)
        assert s.get_emotions() == ["happy", "calm", "excited"]

    def test_returns_default_without_config(self):
        s = Settings(None)
        assert s.get_emotions() == ["neutral"]

    def test_returns_empty_list_from_config(self):
        cfg = make_mock_config(emotion_preset_names=[])
        s = Settings(cfg)
        assert s.get_emotions() == []


# ---------------------------------------------------------------------------
# get_voices()
# ---------------------------------------------------------------------------

class TestGetVoices:
    def test_returns_preset_names_from_config(self):
        cfg = make_mock_config(voice_preset_names=["sage", "noa", "teo", "alloy"])
        s = Settings(cfg)
        assert s.get_voices() == ["sage", "noa", "teo", "alloy"]

    def test_returns_default_without_config(self):
        s = Settings(None)
        assert s.get_voices() == ["sage", "ballad", "alloy"]

    def test_returns_empty_list_from_config(self):
        cfg = make_mock_config(voice_preset_names=[])
        s = Settings(cfg)
        assert s.get_voices() == []


# ---------------------------------------------------------------------------
# get_tts_models() / get_stt_models()
# ---------------------------------------------------------------------------

class TestGetModels:
    def test_tts_models_from_config(self):
        cfg = make_mock_config(tts_model_names=["gpt-4o-mini-tts", "azure/speech/azure-tts"])
        s = Settings(cfg)
        assert s.get_tts_models() == ["gpt-4o-mini-tts", "azure/speech/azure-tts"]

    def test_tts_models_default_without_config(self):
        s = Settings(None)
        assert s.get_tts_models() == ["gpt-4o-mini-tts"]

    def test_stt_models_from_config(self):
        cfg = make_mock_config(stt_model_names=["whisper", "mai-ears-1"])
        s = Settings(cfg)
        assert s.get_stt_models() == ["whisper", "mai-ears-1"]

    def test_stt_models_default_without_config(self):
        s = Settings(None)
        assert s.get_stt_models() == ["whisper"]


# ---------------------------------------------------------------------------
# get_voice_model_pairs()
# ---------------------------------------------------------------------------

class TestGetVoiceModelPairs:
    def test_returns_tuples_from_config(self):
        cfg = make_mock_config(
            voice_preset_names=["sage", "noa"],
            resolve_voice_map={
                "sage": {"provider": "openai", "model": "gpt-4o-mini-tts", "voice": "sage"},
                "noa": {"provider": "openai", "model": "azure/speech/azure-tts", "voice": "en-US-Noa:MAI-Voice-1"},
            },
        )
        s = Settings(cfg)
        pairs = s.get_voice_model_pairs()
        assert pairs == [
            ("sage", "gpt-4o-mini-tts"),
            ("noa", "azure/speech/azure-tts"),
        ]

    def test_calls_resolve_voice_for_each_preset(self):
        cfg = make_mock_config(voice_preset_names=["sage", "noa", "teo"])
        s = Settings(cfg)
        s.get_voice_model_pairs()
        assert cfg.resolve_voice.call_count == 3
        cfg.resolve_voice.assert_any_call("sage")
        cfg.resolve_voice.assert_any_call("noa")
        cfg.resolve_voice.assert_any_call("teo")

    def test_returns_default_without_config(self):
        s = Settings(None)
        pairs = s.get_voice_model_pairs()
        assert pairs == [("sage", "gpt-4o-mini-tts")]

    def test_returns_empty_for_no_voices(self):
        cfg = make_mock_config(voice_preset_names=[])
        s = Settings(cfg)
        assert s.get_voice_model_pairs() == []

    def test_single_voice_preset(self):
        cfg = make_mock_config(
            voice_preset_names=["teo"],
            resolve_voice_map={
                "teo": {"provider": "openai", "model": "azure/speech/azure-tts", "voice": "en-US-Teo:MAI-Voice-1"},
            },
        )
        s = Settings(cfg)
        pairs = s.get_voice_model_pairs()
        assert pairs == [("teo", "azure/speech/azure-tts")]


# ---------------------------------------------------------------------------
# set_voice_and_model()
# ---------------------------------------------------------------------------

class TestSetVoiceAndModel:
    def test_sets_voice_by_preset_and_saves(self):
        cfg = make_mock_config()
        s = Settings(cfg)
        s.set_voice_and_model("noa", "azure/speech/azure-tts")
        cfg.set_tts_voice.assert_called_once_with("noa")
        cfg.save.assert_called_once()

    def test_model_arg_is_ignored(self):
        """Model is inferred from preset, so explicit model arg doesn't matter."""
        cfg = make_mock_config()
        s = Settings(cfg)
        s.set_voice_and_model("sage", "irrelevant-model")
        cfg.set_tts_voice.assert_called_once_with("sage")
        cfg.save.assert_called_once()

    def test_noop_without_config(self):
        s = Settings(None)
        s.set_voice_and_model("sage", "gpt-4o-mini-tts")  # should not raise


# ---------------------------------------------------------------------------
# apply_to_env()
# ---------------------------------------------------------------------------

class TestApplyToEnv:
    def test_pushes_speed_to_env_with_config(self, monkeypatch):
        monkeypatch.delenv("TTS_SPEED", raising=False)
        cfg = make_mock_config(tts_speed=1.3)
        s = Settings(cfg)
        s.apply_to_env()
        assert os.environ["TTS_SPEED"] == "1.3"

    def test_pushes_speed_to_env_without_config(self, monkeypatch):
        monkeypatch.delenv("TTS_SPEED", raising=False)
        s = Settings(None)
        s.apply_to_env()
        # Without config, speed defaults to 1.0
        assert os.environ["TTS_SPEED"] == "1.0"

    def test_pushes_speed_from_env_fallback(self, monkeypatch):
        monkeypatch.setenv("TTS_SPEED", "2.0")
        s = Settings(None)
        s.apply_to_env()
        # Should read TTS_SPEED=2.0 and write it back
        assert os.environ["TTS_SPEED"] == "2.0"

    def test_overwrites_existing_env_with_config(self, monkeypatch):
        monkeypatch.setenv("TTS_SPEED", "0.5")
        cfg = make_mock_config(tts_speed=1.8)
        s = Settings(cfg)
        s.apply_to_env()
        assert os.environ["TTS_SPEED"] == "1.8"


# ---------------------------------------------------------------------------
# toggle_fast()
# ---------------------------------------------------------------------------

class TestToggleFast:
    def test_first_toggle_sets_fast(self):
        cfg = make_mock_config(tts_speed=1.0)
        s = Settings(cfg)
        msg = s.toggle_fast()
        assert msg == "Speed set to 1.8"
        cfg.set_tts_speed.assert_called_once_with(1.8)

    def test_second_toggle_restores_speed(self):
        cfg = make_mock_config(tts_speed=1.0)
        s = Settings(cfg)

        # First toggle: save current and set to 1.8
        s.toggle_fast()

        # Reset mock to track second call
        cfg.set_tts_speed.reset_mock()
        cfg.save.reset_mock()

        # After first toggle, speed property still reads from config mock.
        # The settings module reads self.speed (which reads config.tts_speed)
        # for the restored speed message. Since _pre_fast_speed was set to 1.0:
        msg = s.toggle_fast()

        cfg.set_tts_speed.assert_called_once_with(1.0)
        # After restoring, msg reports the new speed which comes from self.speed
        assert "Speed reset to" in msg

    def test_toggle_fast_preserves_custom_speed(self):
        cfg = make_mock_config(tts_speed=1.4)
        s = Settings(cfg)

        # First toggle: saves 1.4, sets 1.8
        msg1 = s.toggle_fast()
        assert msg1 == "Speed set to 1.8"
        assert s._pre_fast_speed == 1.4

        # Second toggle: restores 1.4
        cfg.set_tts_speed.reset_mock()
        msg2 = s.toggle_fast()
        cfg.set_tts_speed.assert_called_once_with(1.4)
        assert s._pre_fast_speed is None

    def test_toggle_fast_calls_save_each_time(self):
        cfg = make_mock_config(tts_speed=1.0)
        s = Settings(cfg)
        s.toggle_fast()
        assert cfg.save.call_count == 1
        s.toggle_fast()
        assert cfg.save.call_count == 2

    def test_toggle_fast_without_config(self, monkeypatch):
        """toggle_fast works even without config (env-based speed)."""
        monkeypatch.setenv("TTS_SPEED", "1.2")
        s = Settings(None)
        msg = s.toggle_fast()
        assert msg == "Speed set to 1.8"
        assert s._pre_fast_speed == 1.2

    def test_toggle_fast_back_without_config(self, monkeypatch):
        monkeypatch.setenv("TTS_SPEED", "1.2")
        s = Settings(None)
        s.toggle_fast()
        msg = s.toggle_fast()
        # Without config, setter is a no-op, but speed reads from env
        assert "Speed reset to" in msg
        assert s._pre_fast_speed is None


# ---------------------------------------------------------------------------
# toggle_voice()
# ---------------------------------------------------------------------------

class TestToggleVoice:
    def test_cycles_to_next_voice(self):
        cfg = make_mock_config(
            tts_voice_preset="sage",
            voice_preset_names=["sage", "noa", "teo"],
        )
        s = Settings(cfg)
        msg = s.toggle_voice()
        # sage → noa
        cfg.set_tts_voice.assert_called_once_with("noa")
        cfg.save.assert_called_once()

    def test_cycles_wrap_around(self):
        cfg = make_mock_config(
            tts_voice_preset="teo",
            voice_preset_names=["sage", "noa", "teo"],
        )
        s = Settings(cfg)
        msg = s.toggle_voice()
        # teo is last → wraps to sage
        cfg.set_tts_voice.assert_called_once_with("sage")

    def test_voice_not_in_list_selects_first(self):
        cfg = make_mock_config(
            tts_voice_preset="unknown",
            voice_preset_names=["sage", "noa", "teo"],
        )
        s = Settings(cfg)
        msg = s.toggle_voice()
        cfg.set_tts_voice.assert_called_once_with("sage")

    def test_empty_voices_list(self):
        cfg = make_mock_config(
            tts_voice_preset="sage",
            voice_preset_names=[],
        )
        s = Settings(cfg)
        msg = s.toggle_voice()
        assert msg == "No voices available"
        cfg.set_tts_voice.assert_not_called()
        cfg.save.assert_not_called()

    def test_single_voice_cycles_to_itself(self):
        cfg = make_mock_config(
            tts_voice_preset="sage",
            voice_preset_names=["sage"],
        )
        s = Settings(cfg)
        msg = s.toggle_voice()
        # (0 + 1) % 1 == 0 → back to sage
        cfg.set_tts_voice.assert_called_once_with("sage")

    def test_return_message_includes_voice_name(self):
        cfg = make_mock_config(
            tts_voice_preset="sage",
            voice_preset_names=["sage", "noa"],
        )
        s = Settings(cfg)
        # After setting voice to "noa", self.voice reads cfg.tts_voice_preset
        # which is still "sage" (mock attribute). The return message uses self.voice.
        msg = s.toggle_voice()
        assert "Voice:" in msg

    def test_toggle_voice_without_config(self):
        s = Settings(None)
        msg = s.toggle_voice()
        # Without config, get_voices() returns hardcoded ["sage", "ballad", "alloy"]
        # voice defaults to env or "sage", so it should cycle to "ballad"
        assert "Voice:" in msg


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:
    def test_none_config_init(self):
        """Settings can be initialized with None config without error."""
        s = Settings(None)
        assert s._config is None
        assert s._pre_fast_speed is None

    def test_explicit_none_config(self, monkeypatch):
        monkeypatch.delenv("TTS_SPEED", raising=False)
        monkeypatch.delenv("OPENAI_TTS_VOICE", raising=False)
        s = Settings(config=None)
        assert s.speed == 1.0  # default without env
        assert s.voice == "sage"
        assert s.tts_model == "gpt-4o-mini-tts"
        assert s.stt_model == "whisper"
        assert s.emotion == "neutral"

    def test_config_attribute_accessed(self):
        """Verify Settings accesses the right config attributes."""
        cfg = make_mock_config()
        s = Settings(cfg)
        # Access each property to ensure correct delegation
        _ = s.speed  # reads cfg.tts_speed
        _ = s.voice  # reads cfg.tts_voice_preset
        _ = s.tts_model  # reads cfg.tts_model_name
        _ = s.stt_model  # reads cfg.stt_model_name
        _ = s.emotion  # reads cfg.tts_emotion

    def test_setter_saves_after_mutation(self):
        """Each setter must call save() AFTER the mutation method."""
        cfg = make_mock_config()
        # Track call order
        call_log = []
        cfg.set_tts_speed.side_effect = lambda v: call_log.append("set_speed")
        cfg.save.side_effect = lambda: call_log.append("save")

        s = Settings(cfg)
        s.speed = 2.0
        assert call_log == ["set_speed", "save"]

    def test_multiple_setters_each_save(self):
        """Each setter independently calls save."""
        cfg = make_mock_config()
        s = Settings(cfg)
        s.speed = 1.5
        s.voice = "noa"
        s.emotion = "excited"
        assert cfg.save.call_count == 3

    def test_voice_model_pairs_uses_model_key(self):
        """get_voice_model_pairs extracts the 'model' key from resolve_voice."""
        cfg = make_mock_config(
            voice_preset_names=["custom"],
            resolve_voice_map={
                "custom": {
                    "provider": "custom-provider",
                    "model": "custom-model-v2",
                    "voice": "custom-voice",
                },
            },
        )
        s = Settings(cfg)
        pairs = s.get_voice_model_pairs()
        assert pairs == [("custom", "custom-model-v2")]

    def test_toggle_fast_idempotent_after_double_toggle(self):
        """Two fast toggles return to original state."""
        cfg = make_mock_config(tts_speed=1.2)
        s = Settings(cfg)
        s.toggle_fast()  # saves 1.2, sets 1.8
        s.toggle_fast()  # restores 1.2
        assert s._pre_fast_speed is None
        # Last set_tts_speed call was with 1.2
        assert cfg.set_tts_speed.call_args_list[-1] == call(1.2)

    def test_speed_env_var_float_parsing(self, monkeypatch):
        """Speed correctly parses float from env."""
        monkeypatch.setenv("TTS_SPEED", "0.75")
        s = Settings(None)
        assert s.speed == 0.75

    def test_toggle_voice_from_middle_of_list(self):
        """Toggle from a voice in the middle goes to next."""
        cfg = make_mock_config(
            tts_voice_preset="noa",
            voice_preset_names=["sage", "noa", "teo", "alloy"],
        )
        s = Settings(cfg)
        s.toggle_voice()
        cfg.set_tts_voice.assert_called_once_with("teo")

    def test_get_voice_model_pairs_preserves_order(self):
        """Pairs are returned in the same order as voice_preset_names."""
        names = ["teo", "sage", "noa"]
        resolve_map = {
            "teo": {"provider": "openai", "model": "azure/speech/azure-tts", "voice": "en-US-Teo:MAI-Voice-1"},
            "sage": {"provider": "openai", "model": "gpt-4o-mini-tts", "voice": "sage"},
            "noa": {"provider": "openai", "model": "azure/speech/azure-tts", "voice": "en-US-Noa:MAI-Voice-1"},
        }
        cfg = make_mock_config(voice_preset_names=names, resolve_voice_map=resolve_map)
        s = Settings(cfg)
        pairs = s.get_voice_model_pairs()
        assert [p[0] for p in pairs] == ["teo", "sage", "noa"]
        assert pairs[0] == ("teo", "azure/speech/azure-tts")
        assert pairs[1] == ("sage", "gpt-4o-mini-tts")
        assert pairs[2] == ("noa", "azure/speech/azure-tts")
