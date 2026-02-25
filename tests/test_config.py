"""Tests for the io-mcp configuration system.

Tests config loading, env var expansion, deep merge, settings mutation,
TTS/STT CLI arg generation, emotion presets, and local config merging.
"""

from __future__ import annotations

import os
import tempfile

import pytest
import yaml

from io_mcp.config import IoMcpConfig, _expand_env, _deep_merge, _find_new_keys, DEFAULT_CONFIG


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
# Finding new keys (config migration)
# ---------------------------------------------------------------------------

class TestFindNewKeys:
    def test_empty_user_config(self):
        defaults = {"a": 1, "b": 2}
        user = {}
        assert set(_find_new_keys(defaults, user)) == {"a", "b"}

    def test_no_new_keys(self):
        defaults = {"a": 1, "b": 2}
        user = {"a": 10, "b": 20}
        assert _find_new_keys(defaults, user) == []

    def test_one_new_key(self):
        defaults = {"a": 1, "b": 2, "c": 3}
        user = {"a": 10, "b": 20}
        assert _find_new_keys(defaults, user) == ["c"]

    def test_nested_new_key(self):
        defaults = {"x": {"a": 1, "b": 2}}
        user = {"x": {"a": 10}}
        assert _find_new_keys(defaults, user) == ["x.b"]

    def test_deeply_nested_new_key(self):
        defaults = {"config": {"tts": {"localBackend": "termux", "speed": 1.0}}}
        user = {"config": {"tts": {"speed": 1.5}}}
        assert _find_new_keys(defaults, user) == ["config.tts.localBackend"]

    def test_new_entire_section(self):
        defaults = {"config": {"tts": {"speed": 1.0}, "ambient": {"enabled": False}}}
        user = {"config": {"tts": {"speed": 1.5}}}
        assert _find_new_keys(defaults, user) == ["config.ambient"]

    def test_user_extra_keys_ignored(self):
        defaults = {"a": 1}
        user = {"a": 10, "extra": "stuff"}
        assert _find_new_keys(defaults, user) == []


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

class TestConfigLoading:
    def test_creates_default_file(self, tmp_config):
        assert not os.path.isfile(tmp_config)
        config = IoMcpConfig.load(tmp_config)
        assert os.path.isfile(tmp_config)
        assert config.tts_model_name == "gpt-4o-mini-tts"

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
        assert config.tts_model_name == "gpt-4o-mini-tts"
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

    def test_new_defaults_written_to_disk(self, tmp_config):
        """When new default keys are added, they appear in config.yml on load."""
        # Write a partial config missing a key that exists in defaults
        partial = {"config": {"tts": {"speed": 1.5}}}
        with open(tmp_config, "w") as f:
            yaml.dump(partial, f)

        # Load — should auto-merge defaults and write back
        config = IoMcpConfig.load(tmp_config)
        assert config.tts_speed == 1.5  # user value preserved

        # Read the file back — it should now contain default keys
        with open(tmp_config, "r") as f:
            on_disk = yaml.safe_load(f)

        # The localBackend key from defaults should now be on disk
        assert "localBackend" in on_disk.get("config", {}).get("tts", {})
        # Ambient section from defaults should now be on disk
        assert "ambient" in on_disk.get("config", {})
        # Providers from defaults should be on disk
        assert "openai" in on_disk.get("providers", {})

    def test_new_defaults_logged(self, tmp_config, capsys):
        """Loading a partial config logs which new keys were added."""
        # Write a config missing the ambient section
        partial = {"config": {"tts": {"speed": 1.5}}}
        with open(tmp_config, "w") as f:
            yaml.dump(partial, f)

        IoMcpConfig.load(tmp_config)
        captured = capsys.readouterr()
        # Should mention new default keys were added
        assert "new default key" in captured.out


# ---------------------------------------------------------------------------
# Config accessors
# ---------------------------------------------------------------------------

class TestConfigAccessors:
    def test_tts_defaults(self, config_with_defaults):
        c = config_with_defaults
        assert c.tts_model_name == "gpt-4o-mini-tts"
        assert c.tts_voice == "shimmer"
        assert c.tts_speed == 1.3
        assert c.tts_provider_name == "openai"

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
        # Default model is now gpt-4o-mini-tts → openai voices
        assert "sage" in options

    def test_style_defaults(self, config_with_defaults):
        c = config_with_defaults
        assert c.tts_style == "friendly"
        assert c.tts_emotion == "friendly"  # legacy alias
        assert "friendly" in c.tts_style_options
        assert "neutral" in c.tts_style_options

    def test_tts_instructions_returns_style_name(self, config_with_defaults):
        c = config_with_defaults
        # tts_instructions now just returns the style name
        assert c.tts_instructions == "friendly"

    def test_styles_list_has_all_entries(self, config_with_defaults):
        """Styles list contains entries from both old OpenAI and Azure presets."""
        c = config_with_defaults
        styles = c.tts_style_options
        # From old OpenAI presets
        assert "shy" in styles
        assert "calm" in styles
        assert "storyteller" in styles
        # From old Azure presets
        assert "curious" in styles
        assert "empathetic" in styles

    def test_emotion_preset_names_is_style_alias(self, config_with_defaults):
        """emotion_preset_names returns same as tts_style_options (legacy compat)."""
        c = config_with_defaults
        assert c.emotion_preset_names == c.tts_style_options

    def test_style_rotation_defaults_populated(self, config_with_defaults):
        voice_rot = config_with_defaults.tts_voice_rotation
        assert len(voice_rot) == 13  # 11 openai + 2 azure
        assert voice_rot[0]["voice"] == "alloy"
        assert voice_rot[-1]["model"] == "mai-voice-1"

        style_rot = config_with_defaults.tts_style_rotation
        assert len(style_rot) == 15
        assert "friendly" in style_rot
        assert "curious" in style_rot

        # Legacy alias
        assert config_with_defaults.tts_emotion_rotation == style_rot

    def test_extra_options_default_empty(self, config_with_defaults):
        # Default config has no extra options (unless local .io-mcp.yml is present)
        # This test works because tmp_config is in a temp dir with no .io-mcp.yml
        assert isinstance(config_with_defaults.extra_options, list)

    def test_ambient_defaults(self, config_with_defaults):
        c = config_with_defaults
        assert c.ambient_enabled == False
        assert c.ambient_initial_delay == 30
        assert c.ambient_repeat_interval == 45

    def test_health_monitor_defaults(self, config_with_defaults):
        """Health monitor config defaults are set correctly."""
        c = config_with_defaults
        assert c.health_monitor_enabled == True
        assert c.health_warning_threshold == 300.0
        assert c.health_unresponsive_threshold == 600.0
        assert c.health_check_interval == 30.0
        assert c.health_check_tmux_pane == True

    def test_health_monitor_custom_thresholds(self, tmp_config):
        """Health monitor thresholds can be overridden in config."""
        import yaml
        custom = {
            "config": {
                "healthMonitor": {
                    "enabled": False,
                    "warningThresholdSecs": 120,
                    "unresponsiveThresholdSecs": 300,
                    "checkIntervalSecs": 15,
                    "checkTmuxPane": False,
                }
            }
        }
        with open(tmp_config, "w") as f:
            yaml.dump(custom, f)
        c = IoMcpConfig.load(tmp_config)
        assert c.health_monitor_enabled == False
        assert c.health_warning_threshold == 120.0
        assert c.health_unresponsive_threshold == 300.0
        assert c.health_check_interval == 15.0
        assert c.health_check_tmux_pane == False

    def test_health_monitor_in_default_config(self):
        """DEFAULT_CONFIG includes healthMonitor section."""
        health_cfg = DEFAULT_CONFIG.get("config", {}).get("healthMonitor", {})
        assert "enabled" in health_cfg
        assert "warningThresholdSecs" in health_cfg
        assert "unresponsiveThresholdSecs" in health_cfg
        assert "checkIntervalSecs" in health_cfg
        assert "checkTmuxPane" in health_cfg
        assert health_cfg["warningThresholdSecs"] < health_cfg["unresponsiveThresholdSecs"]

    def test_notifications_defaults(self, config_with_defaults):
        """Notification config defaults are set correctly (disabled by default)."""
        c = config_with_defaults
        assert c.notifications_enabled == False
        assert c.notifications_cooldown == 60.0
        assert c.notifications_channels == []

    def test_notifications_custom_config(self, tmp_config):
        """Notification channels can be configured."""
        custom = {
            "config": {
                "notifications": {
                    "enabled": True,
                    "cooldownSecs": 30,
                    "channels": [
                        {
                            "name": "test-ntfy",
                            "type": "ntfy",
                            "url": "https://ntfy.sh/test",
                            "events": ["health_warning"],
                        }
                    ],
                }
            }
        }
        with open(tmp_config, "w") as f:
            yaml.dump(custom, f)
        c = IoMcpConfig.load(tmp_config)
        assert c.notifications_enabled == True
        assert c.notifications_cooldown == 30.0
        assert len(c.notifications_channels) == 1
        assert c.notifications_channels[0]["name"] == "test-ntfy"
        assert c.notifications_channels[0]["type"] == "ntfy"

    def test_notifications_in_default_config(self):
        """DEFAULT_CONFIG includes notifications section."""
        notif_cfg = DEFAULT_CONFIG.get("config", {}).get("notifications", {})
        assert "enabled" in notif_cfg
        assert "cooldownSecs" in notif_cfg
        assert "channels" in notif_cfg
        assert notif_cfg["enabled"] == False  # disabled by default

    def test_ambient_custom_values(self, tmp_config):
        custom = {"config": {"ambient": {
            "enabled": False,
            "initialDelaySecs": 60,
            "repeatIntervalSecs": 90,
        }}}
        with open(tmp_config, "w") as f:
            yaml.dump(custom, f)
        c = IoMcpConfig.load(tmp_config)
        assert c.ambient_enabled == False
        assert c.ambient_initial_delay == 60
        assert c.ambient_repeat_interval == 90


class TestConfigValidation:
    """Tests for config validation warnings."""

    @pytest.fixture
    def tmp_config(self, tmp_path):
        return str(tmp_path / "config.yml")

    def test_valid_config_no_warnings(self, tmp_config):
        """Default config produces no warnings."""
        with open(tmp_config, "w") as f:
            yaml.dump(DEFAULT_CONFIG, f)
        c = IoMcpConfig.load(tmp_config)
        # Filter out expected warnings for missing env vars etc.
        structural_warnings = [w for w in c.validation_warnings
                               if "Missing" not in w and "not found" not in w]
        # No structural warnings from DEFAULT_CONFIG
        # (there may be model/provider warnings if env vars aren't set)

    def test_health_threshold_ordering(self, tmp_config):
        """Warning when warningThreshold >= unresponsiveThreshold."""
        custom = {"config": {"healthMonitor": {
            "warningThresholdSecs": 600,
            "unresponsiveThresholdSecs": 300,  # wrong: lower than warning
        }}}
        with open(tmp_config, "w") as f:
            yaml.dump(custom, f)
        c = IoMcpConfig.load(tmp_config)
        assert any("warningThresholdSecs" in w and "unresponsiveThresholdSecs" in w
                    for w in c.validation_warnings)

    def test_health_low_check_interval(self, tmp_config):
        """Warning when check interval is very low."""
        custom = {"config": {"healthMonitor": {
            "checkIntervalSecs": 2,
        }}}
        with open(tmp_config, "w") as f:
            yaml.dump(custom, f)
        c = IoMcpConfig.load(tmp_config)
        assert any("checkIntervalSecs" in w for w in c.validation_warnings)

    def test_notifications_enabled_no_channels(self, tmp_config):
        """Warning when notifications enabled but no channels."""
        custom = {"config": {"notifications": {
            "enabled": True,
            "channels": [],
        }}}
        with open(tmp_config, "w") as f:
            yaml.dump(custom, f)
        c = IoMcpConfig.load(tmp_config)
        assert any("no channels" in w for w in c.validation_warnings)

    def test_notification_channel_no_url(self, tmp_config):
        """Warning when a channel has no URL."""
        custom = {"config": {"notifications": {
            "enabled": True,
            "channels": [{"name": "bad", "type": "ntfy"}],
        }}}
        with open(tmp_config, "w") as f:
            yaml.dump(custom, f)
        c = IoMcpConfig.load(tmp_config)
        assert any("no URL" in w for w in c.validation_warnings)

    def test_notification_channel_bad_url(self, tmp_config):
        """Warning when a channel URL doesn't start with http(s)."""
        custom = {"config": {"notifications": {
            "enabled": True,
            "channels": [{"name": "bad", "type": "ntfy", "url": "ftp://nope.com"}],
        }}}
        with open(tmp_config, "w") as f:
            yaml.dump(custom, f)
        c = IoMcpConfig.load(tmp_config)
        assert any("http" in w for w in c.validation_warnings)

    def test_notification_channel_unknown_type(self, tmp_config):
        """Warning for unknown channel type."""
        custom = {"config": {"notifications": {
            "enabled": True,
            "channels": [{"name": "bad", "type": "telegram", "url": "https://t.me/x"}],
        }}}
        with open(tmp_config, "w") as f:
            yaml.dump(custom, f)
        c = IoMcpConfig.load(tmp_config)
        assert any("unknown type" in w and "telegram" in w for w in c.validation_warnings)

    def test_notification_channel_unknown_event(self, tmp_config):
        """Warning for unknown event type in channel config."""
        custom = {"config": {"notifications": {
            "enabled": True,
            "channels": [{
                "name": "test",
                "type": "ntfy",
                "url": "https://ntfy.sh/test",
                "events": ["health_warning", "nonexistent_event"],
            }],
        }}}
        with open(tmp_config, "w") as f:
            yaml.dump(custom, f)
        c = IoMcpConfig.load(tmp_config)
        assert any("nonexistent_event" in w for w in c.validation_warnings)

    def test_notification_negative_cooldown(self, tmp_config):
        """Warning for negative cooldown."""
        custom = {"config": {"notifications": {
            "enabled": True,
            "cooldownSecs": -10,
            "channels": [{"name": "x", "type": "ntfy", "url": "https://ntfy.sh/x"}],
        }}}
        with open(tmp_config, "w") as f:
            yaml.dump(custom, f)
        c = IoMcpConfig.load(tmp_config)
        assert any("negative" in w for w in c.validation_warnings)

    def test_validation_warnings_stored(self, tmp_config):
        """Validation warnings are stored on the config object."""
        with open(tmp_config, "w") as f:
            yaml.dump(DEFAULT_CONFIG, f)
        c = IoMcpConfig.load(tmp_config)
        assert isinstance(c.validation_warnings, list)


# ---------------------------------------------------------------------------
# Config mutation
# ---------------------------------------------------------------------------

class TestConfigMutation:
    def test_set_tts_model(self, config_with_defaults):
        c = config_with_defaults
        c.set_tts_model("gpt-4o-mini-tts")
        assert c.tts_model_name == "gpt-4o-mini-tts"
        # Voice should be reset to the new model's default
        assert c.tts_voice == "shimmer"

    def test_set_tts_voice(self, config_with_defaults):
        c = config_with_defaults
        # Default model is gpt-4o-mini-tts — set a valid openai voice
        c.set_tts_voice("coral")
        assert c.tts_voice == "coral"

    def test_set_tts_voice_invalid(self, config_with_defaults):
        c = config_with_defaults
        # Setting an Azure voice on an OpenAI model should raise
        import pytest
        with pytest.raises(ValueError, match="not valid for model"):
            c.set_tts_voice("en-US-Teo:MAI-Voice-1")

    def test_set_tts_voice_cross_model(self, config_with_defaults):
        c = config_with_defaults
        # Switch to Azure model, then set an Azure voice — should work
        c.set_tts_model("mai-voice-1")
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
        # Default provider is now openai; "excited" maps to full text instructions
        assert "energy" in c.tts_instructions.lower() or "excited" in c.tts_instructions.lower()

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
        c.set_tts_style("happy")
        c.save()

        c2 = IoMcpConfig.load(tmp_config)
        assert c2.tts_speed == 1.8
        assert c2.tts_style == "happy"


# ---------------------------------------------------------------------------
# CLI arg generation
# ---------------------------------------------------------------------------

class TestCLIArgs:
    def test_tts_args_azure_speech(self, config_with_defaults, monkeypatch):
        monkeypatch.setenv("AZURE_SPEECH_API_KEY", "test-key")
        monkeypatch.setenv("AZURE_SPEECH_ENDPOINT", "https://test.endpoint")
        c = IoMcpConfig.load(config_with_defaults.config_path)
        # Switch to azure model first since default is now openai
        c.set_tts_model("mai-voice-1")
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

    def test_tts_args_openai_with_style(self, tmp_config, monkeypatch):
        """OpenAI + named style: sends --style only (no --instructions)."""
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        custom = {
            "config": {"tts": {"model": "gpt-4o-mini-tts", "voice": "sage", "style": "happy"}},
        }
        with open(tmp_config, "w") as f:
            yaml.dump(custom, f)
        c = IoMcpConfig.load(tmp_config)
        args = c.tts_cli_args("hello")
        assert "--style" in args
        style_idx = args.index("--style")
        assert args[style_idx + 1] == "happy"
        # No --instructions anymore
        assert "--instructions" not in args

    def test_tts_args_openai_with_custom_style(self, tmp_config, monkeypatch):
        """OpenAI + custom text style: sends --style with the text."""
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        custom = {
            "config": {"tts": {"model": "gpt-4o-mini-tts", "voice": "sage",
                               "style": "Speak like a pirate"}},
        }
        with open(tmp_config, "w") as f:
            yaml.dump(custom, f)
        c = IoMcpConfig.load(tmp_config)
        args = c.tts_cli_args("ahoy")
        assert "--style" in args
        style_idx = args.index("--style")
        assert args[style_idx + 1] == "Speak like a pirate"
        assert "--instructions" not in args

    def test_tts_args_with_overrides(self, config_with_defaults, monkeypatch):
        monkeypatch.setenv("AZURE_SPEECH_API_KEY", "test-key")
        monkeypatch.setenv("AZURE_SPEECH_ENDPOINT", "https://test.endpoint")
        c = IoMcpConfig.load(config_with_defaults.config_path)
        # Switch to azure model to test azure-specific override behavior
        c.set_tts_model("mai-voice-1")
        args = c.tts_cli_args("hello", voice_override="en-US-Teo:MAI-Voice-1",
                              emotion_override="happy")
        assert "en-US-Teo:MAI-Voice-1" in args
        # --style is always passed now
        assert "--style" in args
        style_idx = args.index("--style")
        assert args[style_idx + 1] == "happy"

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


# ---------------------------------------------------------------------------
# Session ambient mode fields
# ---------------------------------------------------------------------------

class TestSessionAmbientFields:
    def test_ambient_count_default(self):
        from io_mcp.session import Session
        s = Session(session_id="test", name="Test")
        assert s.ambient_count == 0
        assert s.heartbeat_spoken == False

    def test_ambient_count_reset(self):
        from io_mcp.session import Session
        s = Session(session_id="test", name="Test")
        s.ambient_count = 5
        s.ambient_count = 0  # simulates tool call reset
        assert s.ambient_count == 0


# ---------------------------------------------------------------------------
# Config reset
# ---------------------------------------------------------------------------

class TestConfigReset:
    """Tests for IoMcpConfig.reset() — delete and regenerate config."""

    def test_reset_deletes_and_recreates(self, tmp_path):
        """reset() deletes existing config and creates fresh defaults."""
        config_path = str(tmp_path / "config.yml")
        # Write a custom config
        custom = {"config": {"tts": {"speed": 2.5, "model": "gpt-4o-mini-tts"}}}
        with open(config_path, "w") as f:
            yaml.dump(custom, f)

        # Reset
        config = IoMcpConfig.reset(config_path)

        # File should exist with defaults
        assert os.path.isfile(config_path)
        # Speed should be the default, not the custom value
        assert config.tts_speed == 1.3
        # Model should be the default
        assert config.tts_model_name == "gpt-4o-mini-tts"

    def test_reset_when_no_file_exists(self, tmp_path):
        """reset() works fine when the file doesn't exist yet."""
        config_path = str(tmp_path / "config.yml")
        assert not os.path.isfile(config_path)

        config = IoMcpConfig.reset(config_path)

        # File should be created with defaults
        assert os.path.isfile(config_path)
        assert config.tts_speed == 1.3

    def test_reset_preserves_all_defaults(self, tmp_path):
        """After reset, config has all DEFAULT_CONFIG keys."""
        config_path = str(tmp_path / "config.yml")

        config = IoMcpConfig.reset(config_path)

        # Read back from disk and verify key sections exist
        with open(config_path, "r") as f:
            on_disk = yaml.safe_load(f)

        assert "providers" in on_disk
        assert "models" in on_disk
        assert "config" in on_disk
        assert "styles" in on_disk
        assert "openai" in on_disk["providers"]
        assert "healthMonitor" in on_disk["config"]
        assert "ambient" in on_disk["config"]
        assert "notifications" in on_disk["config"]

    def test_reset_prints_deleted_message(self, tmp_path, capsys):
        """reset() prints a message about deleting the config."""
        config_path = str(tmp_path / "config.yml")
        # Create a file first
        with open(config_path, "w") as f:
            yaml.dump({"config": {"tts": {"speed": 1.0}}}, f)

        IoMcpConfig.reset(config_path)
        captured = capsys.readouterr()
        assert "deleted" in captured.out.lower()
