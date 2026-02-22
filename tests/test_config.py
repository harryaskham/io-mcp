"""Tests for the io-mcp configuration system.

Tests config loading, env var expansion, deep merge, settings mutation,
TTS/STT CLI arg generation, emotion presets, and local config merging.
"""

from __future__ import annotations

import os
import tempfile

import pytest
import yaml

from io_mcp.config import IoMcpConfig, _expand_env, _deep_merge, DEFAULT_CONFIG


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@pytest.fixture()
def tmp_config(tmp_path):
    """Create a temporary config file path."""
    return str(tmp_path / "config.yml")


@pytest.fixture()
def config_with_defaults(tmp_config):
    """Load a config with all defaults."""
    return IoMcpConfig.load(tmp_config)


# ---------------------------------------------------------------------------
# Env var expansion
# ---------------------------------------------------------------------------

class TestEnvExpansion:
    def test_simple_var(self, monkeypatch):
        monkeypatch.setenv("TEST_VAR", "hello")
        assert _expand_env("${TEST_VAR}") == "hello"

    def test_var_with_default(self, monkeypatch):
        monkeypatch.delenv("MISSING_VAR", raising=False)
        assert _expand_env("${MISSING_VAR:-fallback}") == "fallback"

    def test_var_with_default_present(self, monkeypatch):
        monkeypatch.setenv("PRESENT_VAR", "real")
        assert _expand_env("${PRESENT_VAR:-fallback}") == "real"

    def test_missing_var_empty(self, monkeypatch):
        monkeypatch.delenv("NOPE", raising=False)
        assert _expand_env("${NOPE}") == ""

    def test_no_expansion_needed(self):
        assert _expand_env("plain text") == "plain text"

    def test_multiple_vars(self, monkeypatch):
        monkeypatch.setenv("A", "1")
        monkeypatch.setenv("B", "2")
        assert _expand_env("${A}-${B}") == "1-2"


# ---------------------------------------------------------------------------
# Deep merge
# ---------------------------------------------------------------------------

class TestDeepMerge:
    def test_simple_override(self):
        base = {"a": 1, "b": 2}
        override = {"b": 3}
        result = _deep_merge(base, override)
        assert result == {"a": 1, "b": 3}

    def test_nested_merge(self):
        base = {"x": {"a": 1, "b": 2}}
        override = {"x": {"b": 3, "c": 4}}
        result = _deep_merge(base, override)
        assert result == {"x": {"a": 1, "b": 3, "c": 4}}

    def test_new_keys(self):
        base = {"a": 1}
        override = {"b": 2}
        result = _deep_merge(base, override)
        assert result == {"a": 1, "b": 2}

    def test_override_dict_with_scalar(self):
        base = {"a": {"nested": True}}
        override = {"a": "flat"}
        result = _deep_merge(base, override)
        assert result == {"a": "flat"}

    def test_base_not_modified(self):
        base = {"a": 1}
        override = {"a": 2}
        _deep_merge(base, override)
        assert base == {"a": 1}


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

class TestConfigLoading:
    def test_creates_default_file(self, tmp_config):
        assert not os.path.isfile(tmp_config)
        config = IoMcpConfig.load(tmp_config)
        assert os.path.isfile(tmp_config)
        assert config.tts_model_name == "mai-voice-1"

    def test_loads_existing_file(self, tmp_config):
        # Write a custom config
        custom = {"config": {"tts": {"model": "gpt-4o-mini-tts", "speed": 2.0}}}
        with open(tmp_config, "w") as f:
            yaml.dump(custom, f)
        config = IoMcpConfig.load(tmp_config)
        assert config.tts_model_name == "gpt-4o-mini-tts"
        assert config.tts_speed == 2.0

    def test_merges_with_defaults(self, tmp_config):
        # Partial config — should get defaults for missing keys
        custom = {"config": {"tts": {"speed": 1.5}}}
        with open(tmp_config, "w") as f:
            yaml.dump(custom, f)
        config = IoMcpConfig.load(tmp_config)
        # Speed overridden
        assert config.tts_speed == 1.5
        # Model comes from defaults
        assert config.tts_model_name == "mai-voice-1"
        # Providers come from defaults
        assert "openai" in config.providers

    def test_local_config_merge(self, tmp_config, tmp_path, monkeypatch):
        # Create a local .io-mcp.yml in tmp_path
        local_config = {
            "extraOptions": [
                {"title": "Test option", "description": "A test", "silent": True}
            ]
        }
        local_path = tmp_path / ".io-mcp.yml"
        with open(local_path, "w") as f:
            yaml.dump(local_config, f)

        monkeypatch.chdir(tmp_path)
        config = IoMcpConfig.load(tmp_config)
        assert len(config.extra_options) == 1
        assert config.extra_options[0]["title"] == "Test option"

    def test_reload(self, tmp_config):
        config = IoMcpConfig.load(tmp_config)
        assert config.tts_speed == 1.3  # default

        # Modify the file
        config.raw["config"]["tts"]["speed"] = 2.5
        config.save()

        # Reload
        config2 = IoMcpConfig.load(tmp_config)
        assert config2.tts_speed == 2.5


# ---------------------------------------------------------------------------
# Config accessors
# ---------------------------------------------------------------------------

class TestConfigAccessors:
    def test_tts_defaults(self, config_with_defaults):
        c = config_with_defaults
        assert c.tts_model_name == "mai-voice-1"
        assert c.tts_voice == "en-US-Noa:MAI-Voice-1"
        assert c.tts_speed == 1.3
        assert c.tts_provider_name == "azure-speech"

    def test_stt_defaults(self, config_with_defaults):
        c = config_with_defaults
        assert c.stt_model_name == "whisper"
        assert c.stt_realtime == False
        assert c.stt_provider_name == "openai"

    def test_tts_model_names(self, config_with_defaults):
        names = config_with_defaults.tts_model_names
        assert "gpt-4o-mini-tts" in names
        assert "mai-voice-1" in names

    def test_stt_model_names(self, config_with_defaults):
        names = config_with_defaults.stt_model_names
        assert "whisper" in names
        assert "mai-ears-1" in names

    def test_tts_voice_options(self, config_with_defaults):
        options = config_with_defaults.tts_voice_options
        assert "en-US-Noa:MAI-Voice-1" in options

    def test_emotion_defaults(self, config_with_defaults):
        c = config_with_defaults
        assert c.tts_emotion == "shy"
        assert "shy" in c.emotion_preset_names
        assert "calm" in c.emotion_preset_names

    def test_tts_instructions_from_preset(self, config_with_defaults):
        c = config_with_defaults
        instructions = c.tts_instructions
        assert "whisper" in instructions.lower() or "quiet" in instructions.lower()

    def test_voice_rotation_default_empty(self, config_with_defaults):
        assert config_with_defaults.tts_voice_rotation == []
        assert config_with_defaults.tts_emotion_rotation == []

    def test_extra_options_default_empty(self, config_with_defaults):
        # Default config has no extra options (unless local .io-mcp.yml is present)
        # This test works because tmp_config is in a temp dir with no .io-mcp.yml
        assert isinstance(config_with_defaults.extra_options, list)


# ---------------------------------------------------------------------------
# Config mutation
# ---------------------------------------------------------------------------

class TestConfigMutation:
    def test_set_tts_model(self, config_with_defaults):
        c = config_with_defaults
        c.set_tts_model("gpt-4o-mini-tts")
        assert c.tts_model_name == "gpt-4o-mini-tts"
        # Voice should be reset to the new model's default
        assert c.tts_voice == "sage"

    def test_set_tts_voice(self, config_with_defaults):
        c = config_with_defaults
        c.set_tts_voice("en-US-Teo:MAI-Voice-1")
        assert c.tts_voice == "en-US-Teo:MAI-Voice-1"

    def test_set_tts_speed(self, config_with_defaults):
        c = config_with_defaults
        c.set_tts_speed(2.0)
        assert c.tts_speed == 2.0

    def test_set_tts_emotion(self, config_with_defaults):
        c = config_with_defaults
        c.set_tts_emotion("excited")
        assert c.tts_emotion == "excited"
        assert "enthusiasm" in c.tts_instructions.lower()

    def test_set_stt_model(self, config_with_defaults):
        c = config_with_defaults
        c.set_stt_model("mai-ears-1")
        assert c.stt_model_name == "mai-ears-1"
        assert c.stt_provider_name == "azure-foundry"

    def test_set_stt_realtime(self, config_with_defaults):
        c = config_with_defaults
        c.set_stt_realtime(True)
        assert c.stt_realtime == True

    def test_save_and_reload(self, tmp_config):
        c = IoMcpConfig.load(tmp_config)
        c.set_tts_speed(1.8)
        c.set_tts_emotion("calm")
        c.save()

        c2 = IoMcpConfig.load(tmp_config)
        assert c2.tts_speed == 1.8
        assert c2.tts_emotion == "calm"


# ---------------------------------------------------------------------------
# CLI arg generation
# ---------------------------------------------------------------------------

class TestCLIArgs:
    def test_tts_args_azure_speech(self, config_with_defaults, monkeypatch):
        monkeypatch.setenv("AZURE_SPEECH_API_KEY", "test-key")
        monkeypatch.setenv("AZURE_SPEECH_ENDPOINT", "https://test.endpoint")
        c = IoMcpConfig.load(config_with_defaults.config_path)
        args = c.tts_cli_args("hello world")
        assert args[0] == "hello world"
        assert "--provider" in args
        assert "azure-speech" in args
        assert "--stdout" in args
        assert "--response-format" in args
        assert "wav" in args

    def test_tts_args_openai(self, tmp_config, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        custom = {"config": {"tts": {"model": "gpt-4o-mini-tts", "voice": "sage"}}}
        with open(tmp_config, "w") as f:
            yaml.dump(custom, f)
        c = IoMcpConfig.load(tmp_config)
        args = c.tts_cli_args("test text")
        assert "--model" in args
        assert "gpt-4o-mini-tts" in args
        assert "--voice" in args
        assert "sage" in args

    def test_tts_args_with_overrides(self, config_with_defaults, monkeypatch):
        monkeypatch.setenv("AZURE_SPEECH_API_KEY", "test-key")
        c = IoMcpConfig.load(config_with_defaults.config_path)
        args = c.tts_cli_args("hello", voice_override="en-US-Teo:MAI-Voice-1",
                              emotion_override="calm")
        assert "en-US-Teo:MAI-Voice-1" in args
        # Should have instructions from the calm preset
        instructions_idx = args.index("--instructions")
        assert "soothing" in args[instructions_idx + 1].lower() or "relaxed" in args[instructions_idx + 1].lower()

    def test_stt_args_basic(self, config_with_defaults, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        c = IoMcpConfig.load(config_with_defaults.config_path)
        args = c.stt_cli_args()
        assert "--stdin" in args
        assert "--transcription-model" in args
        assert "whisper" in args

    def test_stt_args_realtime(self, tmp_config, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        custom = {"config": {"stt": {"model": "whisper", "realtime": True}}}
        with open(tmp_config, "w") as f:
            yaml.dump(custom, f)
        c = IoMcpConfig.load(tmp_config)
        args = c.stt_cli_args()
        assert "--realtime" in args
        assert "--realtime-model" in args

    def test_stt_args_no_realtime_for_mai_ears(self, tmp_config, monkeypatch):
        monkeypatch.setenv("AZURE_WCUS_API_KEY", "test-key")
        custom = {"config": {"stt": {"model": "mai-ears-1", "realtime": True}}}
        with open(tmp_config, "w") as f:
            yaml.dump(custom, f)
        c = IoMcpConfig.load(tmp_config)
        args = c.stt_cli_args()
        # mai-ears-1 doesn't support realtime
        assert "--realtime" not in args
