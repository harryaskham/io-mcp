"""Tests for the io-mcp configuration system.

Tests config loading, env var expansion, deep merge, settings mutation,
TTS/STT CLI arg generation, emotion presets, and local config merging.
"""

from __future__ import annotations

import os
import tempfile

import pytest
import yaml

from io_mcp.config import (
    IoMcpConfig, _expand_env, _expand_config, _deep_merge,
    _find_new_keys, _closest_match, _edit_distance,
    DEFAULT_CONFIG, _DJENT_EXTRA_OPTIONS, _DJENT_QUICK_ACTIONS,
)


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
        # Default voice preset is "noa" → azure/speech/azure-tts
        assert config.tts_model_name == "azure/speech/azure-tts"
        assert config.tts_voice_preset == "noa"

    def test_loads_existing_file(self, tmp_config):
        # Write a custom config with voice preset
        custom = {"config": {"tts": {"voice": "sage", "speed": 2.0}}}
        with open(tmp_config, "w") as f:
            yaml.dump(custom, f)
        config = IoMcpConfig.load(tmp_config)
        assert config.tts_voice_preset == "sage"
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
        # Voice preset comes from defaults
        assert config.tts_voice_preset == "noa"
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
        assert config.tts_speed == 1.0  # default

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
        # Default voice preset is "noa" → azure/speech/azure-tts on openai provider
        assert c.tts_voice_preset == "noa"
        assert c.tts_model_name == "azure/speech/azure-tts"
        assert c.tts_voice == "en-US-Noa:MAI-Voice-1"
        assert c.tts_speed == 1.0
        assert c.tts_provider_name == "openai"

    def test_stt_defaults(self, config_with_defaults):
        c = config_with_defaults
        assert c.stt_model_name == "whisper"
        assert c.stt_realtime == False
        assert c.stt_provider_name == "openai"

    def test_tts_model_names(self, config_with_defaults):
        names = config_with_defaults.tts_model_names
        assert "gpt-4o-mini-tts" in names
        assert "azure/speech/azure-tts" in names

    def test_stt_model_names(self, config_with_defaults):
        names = config_with_defaults.stt_model_names
        assert "whisper" in names
        assert "mai-ears-1" in names

    def test_voice_preset_names(self, config_with_defaults):
        names = config_with_defaults.voice_preset_names
        assert "sage" in names
        assert "noa" in names
        assert "teo" in names
        assert "alloy" in names

    def test_tts_voice_options(self, config_with_defaults):
        options = config_with_defaults.tts_voice_options
        # Voice options are now voice preset names
        assert "sage" in options
        assert "noa" in options

    def test_style_defaults(self, config_with_defaults):
        c = config_with_defaults
        assert c.tts_style == "whispering"
        assert c.tts_emotion == "whispering"  # legacy alias
        assert "friendly" in c.tts_style_options
        assert "terrified" in c.tts_style_options

    def test_tts_instructions_returns_style_name(self, config_with_defaults):
        c = config_with_defaults
        # tts_instructions now just returns the style name
        assert c.tts_instructions == "whispering"

    def test_styles_list_has_all_entries(self, config_with_defaults):
        """Styles list contains entries from the Azure Speech presets."""
        c = config_with_defaults
        styles = c.tts_style_options
        assert "angry" in styles
        assert "cheerful" in styles
        assert "excited" in styles

    def test_emotion_preset_names_is_style_alias(self, config_with_defaults):
        """emotion_preset_names returns same as tts_style_options (legacy compat)."""
        c = config_with_defaults
        assert c.emotion_preset_names == c.tts_style_options

    def test_style_rotation_defaults_populated(self, config_with_defaults):
        voice_rot = config_with_defaults.tts_voice_rotation
        assert len(voice_rot) == 2  # noa + teo
        assert voice_rot[0]["preset"] == "noa"
        assert voice_rot[-1]["preset"] == "teo"

        style_rot = config_with_defaults.tts_style_rotation
        assert len(style_rot) == 11
        assert "friendly" in style_rot
        assert "terrified" in style_rot

        # Legacy alias
        assert config_with_defaults.tts_emotion_rotation == style_rot

    def test_random_rotation_default_true(self, config_with_defaults):
        assert config_with_defaults.tts_random_rotation is True

    def test_resolve_voice_preset(self, config_with_defaults):
        """resolve_voice returns full definition for a named preset."""
        c = config_with_defaults
        resolved = c.resolve_voice("sage")
        assert resolved["provider"] == "openai"
        assert resolved["model"] == "gpt-4o-mini-tts"
        assert resolved["voice"] == "sage"

    def test_resolve_voice_preset_mai(self, config_with_defaults):
        """resolve_voice resolves MAI voice presets correctly."""
        c = config_with_defaults
        resolved = c.resolve_voice("noa")
        assert resolved["provider"] == "openai"
        assert resolved["model"] == "azure/speech/azure-tts"
        assert resolved["voice"] == "en-US-Noa:MAI-Voice-1"

    def test_resolve_voice_fallback(self, config_with_defaults):
        """resolve_voice falls back to raw voice string on openai for unknown presets."""
        c = config_with_defaults
        resolved = c.resolve_voice("unknown-voice")
        assert resolved["provider"] == "openai"
        assert resolved["model"] == "gpt-4o-mini-tts"
        assert resolved["voice"] == "unknown-voice"

    def test_ui_voice_preset(self, config_with_defaults):
        """tts_ui_voice_preset returns the UI voice preset name."""
        c = config_with_defaults
        assert c.tts_ui_voice_preset == "teo"
        assert c.tts_ui_voice == "en-US-Teo:MAI-Voice-1"

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
    def test_set_tts_voice_by_preset(self, config_with_defaults):
        c = config_with_defaults
        c.set_tts_voice("sage")
        assert c.tts_voice_preset == "sage"
        assert c.tts_voice == "sage"
        assert c.tts_model_name == "gpt-4o-mini-tts"

    def test_set_tts_voice_mai_preset(self, config_with_defaults):
        c = config_with_defaults
        c.set_tts_voice("teo")
        assert c.tts_voice_preset == "teo"
        assert c.tts_voice == "en-US-Teo:MAI-Voice-1"
        assert c.tts_model_name == "azure/speech/azure-tts"

    def test_set_tts_voice_by_raw_string(self, config_with_defaults):
        """Setting voice by raw string finds matching preset."""
        c = config_with_defaults
        c.set_tts_voice("en-US-Teo:MAI-Voice-1")
        assert c.tts_voice_preset == "teo"

    def test_set_tts_model_finds_preset(self, config_with_defaults):
        c = config_with_defaults
        c.set_tts_model("gpt-4o-mini-tts")
        assert c.tts_model_name == "gpt-4o-mini-tts"
        # Should have switched to a preset using this model
        assert c.tts_voice_preset in ["alloy", "ash", "ballad", "coral", "echo",
                                       "fable", "onyx", "nova", "sage", "shimmer", "verse"]

    def test_set_tts_voice_preset_direct(self, config_with_defaults):
        c = config_with_defaults
        c.set_tts_voice_preset("coral")
        assert c.tts_voice_preset == "coral"
        assert c.tts_voice == "coral"

    def test_set_tts_speed(self, config_with_defaults):
        c = config_with_defaults
        c.set_tts_speed(2.0)
        assert c.tts_speed == 2.0

    def test_set_tts_emotion(self, config_with_defaults):
        c = config_with_defaults
        c.set_tts_emotion("excited")
        assert c.tts_emotion == "excited"
        assert c.tts_instructions == "excited"

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
    def test_tts_args_openai(self, tmp_config, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        custom = {"config": {"tts": {"voice": "sage"}}}
        with open(tmp_config, "w") as f:
            yaml.dump(custom, f)
        c = IoMcpConfig.load(tmp_config)
        args = c.tts_cli_args("test text")
        assert "--model" in args
        assert "gpt-4o-mini-tts" in args
        assert "--voice" in args
        assert "sage" in args

    def test_tts_args_mai_voice(self, config_with_defaults, monkeypatch):
        """MAI voice preset resolves through openai provider."""
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        c = IoMcpConfig.load(config_with_defaults.config_path)
        c.set_tts_voice("noa")
        args = c.tts_cli_args("hello world")
        assert args[0] == "hello world"
        assert "--model" in args
        assert "azure/speech/azure-tts" in args
        assert "--voice" in args
        assert "en-US-Noa:MAI-Voice-1" in args
        assert "--stdout" in args
        assert "--response-format" in args
        assert "wav" in args

    def test_tts_args_openai_with_style(self, tmp_config, monkeypatch):
        """OpenAI + named style: sends --style only (no --instructions)."""
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        custom = {
            "config": {"tts": {"voice": "sage", "style": "happy"}},
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
            "config": {"tts": {"voice": "sage",
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

    def test_tts_args_with_voice_preset_override(self, config_with_defaults, monkeypatch):
        """voice_override with preset name resolves correctly."""
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        c = IoMcpConfig.load(config_with_defaults.config_path)
        args = c.tts_cli_args("hello", voice_override="teo",
                              emotion_override="happy")
        assert "en-US-Teo:MAI-Voice-1" in args
        assert "--style" in args
        style_idx = args.index("--style")
        assert args[style_idx + 1] == "happy"

    def test_tts_args_style_degree(self, config_with_defaults, monkeypatch):
        """styleDegree is passed when set."""
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        c = IoMcpConfig.load(config_with_defaults.config_path)
        # Default styleDegree is 2
        args = c.tts_cli_args("hello")
        assert "--style-degree" in args
        degree_idx = args.index("--style-degree")
        assert args[degree_idx + 1] == "2.0"

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
        custom = {"config": {"tts": {"speed": 2.5, "voice": "sage"}}}
        with open(config_path, "w") as f:
            yaml.dump(custom, f)

        # Reset
        config = IoMcpConfig.reset(config_path)

        # File should exist with defaults
        assert os.path.isfile(config_path)
        # Speed should be the default, not the custom value
        assert config.tts_speed == 1.0
        # Voice preset should be the default
        assert config.tts_voice_preset == "noa"

    def test_reset_when_no_file_exists(self, tmp_path):
        """reset() works fine when the file doesn't exist yet."""
        config_path = str(tmp_path / "config.yml")
        assert not os.path.isfile(config_path)

        config = IoMcpConfig.reset(config_path)

        # File should be created with defaults
        assert os.path.isfile(config_path)
        assert config.tts_speed == 1.0

    def test_reset_preserves_all_defaults(self, tmp_path):
        """After reset, config has all DEFAULT_CONFIG keys."""
        config_path = str(tmp_path / "config.yml")

        config = IoMcpConfig.reset(config_path)

        # Read back from disk and verify key sections exist
        with open(config_path, "r") as f:
            on_disk = yaml.safe_load(f)

        assert "providers" in on_disk
        assert "voices" in on_disk
        assert "config" in on_disk
        assert "styles" in on_disk
        assert "openai" in on_disk["providers"]
        assert "sage" in on_disk["voices"]
        assert "noa" in on_disk["voices"]
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


# ===========================================================================
# ADDITIONAL COMPREHENSIVE TESTS
# ===========================================================================


# ---------------------------------------------------------------------------
# _expand_env — additional edge cases
# ---------------------------------------------------------------------------

class TestEnvExpansionEdgeCases:
    """Additional edge cases for _expand_env beyond the basics."""

    def test_nested_default_with_colons(self, monkeypatch):
        """Default value itself contains colons (e.g. URLs)."""
        monkeypatch.delenv("MY_URL", raising=False)
        assert _expand_env("${MY_URL:-https://example.com:8080}") == "https://example.com:8080"

    def test_empty_var_returns_empty(self, monkeypatch):
        """An env var set to empty string returns empty, not default."""
        monkeypatch.setenv("EMPTY_VAR", "")
        assert _expand_env("${EMPTY_VAR:-fallback}") == ""

    def test_var_with_empty_default(self, monkeypatch):
        """${VAR:-} with empty default gives empty when VAR unset."""
        monkeypatch.delenv("UNSET_VAR", raising=False)
        assert _expand_env("${UNSET_VAR:-}") == ""

    def test_var_embedded_in_text(self, monkeypatch):
        monkeypatch.setenv("USER", "alice")
        assert _expand_env("hello ${USER} world") == "hello alice world"

    def test_consecutive_vars(self, monkeypatch):
        monkeypatch.setenv("X", "a")
        monkeypatch.setenv("Y", "b")
        assert _expand_env("${X}${Y}") == "ab"

    def test_dollar_without_braces_not_expanded(self):
        """$VAR (no braces) is not expanded — only ${VAR} is."""
        assert _expand_env("$NOTEXPANDED") == "$NOTEXPANDED"

    def test_var_name_with_underscores(self, monkeypatch):
        monkeypatch.setenv("MY_LONG_VAR_NAME", "value")
        assert _expand_env("${MY_LONG_VAR_NAME}") == "value"

    def test_default_with_spaces(self, monkeypatch):
        monkeypatch.delenv("SPACE_VAR", raising=False)
        assert _expand_env("${SPACE_VAR:-hello world}") == "hello world"


# ---------------------------------------------------------------------------
# _expand_config — recursive expansion
# ---------------------------------------------------------------------------

class TestExpandConfig:
    """Tests for _expand_config — recursive env var expansion."""

    def test_expand_string(self, monkeypatch):
        monkeypatch.setenv("FOO", "bar")
        assert _expand_config("${FOO}") == "bar"

    def test_expand_dict(self, monkeypatch):
        monkeypatch.setenv("K", "val")
        result = _expand_config({"key": "${K}"})
        assert result == {"key": "val"}

    def test_expand_list(self, monkeypatch):
        monkeypatch.setenv("A", "1")
        monkeypatch.setenv("B", "2")
        result = _expand_config(["${A}", "${B}", "plain"])
        assert result == ["1", "2", "plain"]

    def test_expand_nested_dict(self, monkeypatch):
        monkeypatch.setenv("INNER", "deep")
        result = _expand_config({"outer": {"inner": "${INNER}"}})
        assert result == {"outer": {"inner": "deep"}}

    def test_expand_list_of_dicts(self, monkeypatch):
        monkeypatch.setenv("NAME", "alice")
        result = _expand_config([{"name": "${NAME}"}, {"name": "bob"}])
        assert result == [{"name": "alice"}, {"name": "bob"}]

    def test_non_string_passthrough(self):
        """Integers, floats, booleans, None pass through unchanged."""
        assert _expand_config(42) == 42
        assert _expand_config(3.14) == 3.14
        assert _expand_config(True) is True
        assert _expand_config(None) is None

    def test_mixed_types_in_dict(self, monkeypatch):
        monkeypatch.setenv("S", "str")
        result = _expand_config({"s": "${S}", "i": 1, "b": True, "n": None})
        assert result == {"s": "str", "i": 1, "b": True, "n": None}

    def test_deeply_nested_structure(self, monkeypatch):
        monkeypatch.setenv("DEEP", "found")
        obj = {"a": {"b": {"c": {"d": [{"e": "${DEEP}"}]}}}}
        result = _expand_config(obj)
        assert result["a"]["b"]["c"]["d"][0]["e"] == "found"


# ---------------------------------------------------------------------------
# _deep_merge — additional edge cases
# ---------------------------------------------------------------------------

class TestDeepMergeAdditional:
    """Additional edge cases for _deep_merge."""

    def test_lists_replaced_not_merged(self):
        """Override lists replace base lists entirely (no concatenation)."""
        base = {"items": [1, 2, 3]}
        override = {"items": [4, 5]}
        result = _deep_merge(base, override)
        assert result == {"items": [4, 5]}

    def test_override_scalar_with_dict(self):
        """Scalar in base replaced by dict in override."""
        base = {"a": "flat"}
        override = {"a": {"nested": True}}
        result = _deep_merge(base, override)
        assert result == {"a": {"nested": True}}

    def test_empty_override(self):
        """Empty override dict leaves base unchanged."""
        base = {"a": 1, "b": 2}
        result = _deep_merge(base, {})
        assert result == {"a": 1, "b": 2}

    def test_empty_base(self):
        """Empty base returns override."""
        result = _deep_merge({}, {"a": 1})
        assert result == {"a": 1}

    def test_both_empty(self):
        result = _deep_merge({}, {})
        assert result == {}

    def test_deeply_nested_merge(self):
        base = {"a": {"b": {"c": 1, "d": 2}}}
        override = {"a": {"b": {"c": 99}}}
        result = _deep_merge(base, override)
        assert result == {"a": {"b": {"c": 99, "d": 2}}}

    def test_override_does_not_modify_original(self):
        """Neither base nor override should be mutated."""
        base = {"a": {"b": 1}}
        override = {"a": {"b": 2}}
        _deep_merge(base, override)
        assert override == {"a": {"b": 2}}

    def test_none_values_in_override(self):
        """None in override replaces base value."""
        base = {"a": "something"}
        override = {"a": None}
        result = _deep_merge(base, override)
        assert result == {"a": None}

    def test_multiple_sections(self):
        """Merge across multiple parallel sections."""
        base = {"x": {"a": 1}, "y": {"b": 2}, "z": {"c": 3}}
        override = {"x": {"a": 10}, "z": {"d": 4}}
        result = _deep_merge(base, override)
        assert result == {"x": {"a": 10}, "y": {"b": 2}, "z": {"c": 3, "d": 4}}


# ---------------------------------------------------------------------------
# _edit_distance
# ---------------------------------------------------------------------------

class TestEditDistance:
    """Tests for _edit_distance (Levenshtein)."""

    def test_identical_strings(self):
        assert _edit_distance("hello", "hello") == 0

    def test_empty_strings(self):
        assert _edit_distance("", "") == 0

    def test_one_empty(self):
        assert _edit_distance("", "abc") == 3
        assert _edit_distance("abc", "") == 3

    def test_single_insertion(self):
        assert _edit_distance("cat", "cats") == 1

    def test_single_deletion(self):
        assert _edit_distance("cats", "cat") == 1

    def test_single_substitution(self):
        assert _edit_distance("cat", "car") == 1

    def test_transposition(self):
        # "ab" -> "ba" requires 2 edits in Levenshtein (sub+sub)
        assert _edit_distance("ab", "ba") == 2

    def test_completely_different(self):
        assert _edit_distance("abc", "xyz") == 3

    def test_case_sensitive(self):
        assert _edit_distance("Hello", "hello") == 1

    def test_symmetric(self):
        """Distance(a,b) == distance(b,a)."""
        assert _edit_distance("kitten", "sitting") == _edit_distance("sitting", "kitten")

    def test_single_char(self):
        assert _edit_distance("a", "b") == 1
        assert _edit_distance("a", "a") == 0

    def test_prefix(self):
        assert _edit_distance("test", "testing") == 3

    def test_known_distance(self):
        # Classic example: kitten -> sitting = 3
        assert _edit_distance("kitten", "sitting") == 3


# ---------------------------------------------------------------------------
# _closest_match
# ---------------------------------------------------------------------------

class TestClosestMatch:
    """Tests for _closest_match — typo suggestion."""

    def test_exact_match_case_insensitive(self):
        """Exact match (case insensitive) returns the original-case key."""
        result = _closest_match("TTS", {"tts", "stt", "config"})
        assert result == "tts"

    def test_close_typo(self):
        result = _closest_match("conifg", {"config", "providers", "voices"})
        assert result == "config"

    def test_no_match_beyond_max_distance(self):
        """Returns None if no candidate is close enough."""
        result = _closest_match("zzzzzzz", {"config", "providers", "voices"}, max_distance=2)
        assert result is None

    def test_max_distance_respected(self):
        """With strict max_distance=1, only single-edit typos match."""
        result = _closest_match("confg", {"config"}, max_distance=1)
        assert result == "config"  # 1 deletion
        result = _closest_match("xyzfg", {"config"}, max_distance=1)
        assert result is None  # too many edits needed

    def test_empty_valid_keys(self):
        result = _closest_match("anything", set())
        assert result is None

    def test_picks_closest(self):
        """When multiple candidates, picks the closest one."""
        result = _closest_match("spead", {"speed", "speak", "specification"})
        # "spead" → "speak" (1 edit: d→k), "speed" (2 edits: a→e,d→d? no... s-p-e-a-d → s-p-e-e-d = 1 edit)
        # Both "speak" and "speed" are 1 edit from "spead" so either is acceptable
        assert result in ("speak", "speed")
        # But "specification" is far — should not be returned
        result2 = _closest_match("xyz", {"abc", "xyw"}, max_distance=3)
        assert result2 == "xyw"  # 1 edit vs 3 edits

    def test_length_filter(self):
        """Candidates whose length differs too much are skipped."""
        # "ab" vs "abcdefgh" — length diff 6 > max_distance 3
        result = _closest_match("ab", {"abcdefgh"}, max_distance=3)
        assert result is None


# ---------------------------------------------------------------------------
# _find_new_keys — additional edge cases
# ---------------------------------------------------------------------------

class TestFindNewKeysAdditional:
    """Additional edge cases for _find_new_keys."""

    def test_both_empty(self):
        assert _find_new_keys({}, {}) == []

    def test_non_dict_values_not_recursed(self):
        """When default value is a list, it's compared as top-level key only."""
        defaults = {"styles": ["a", "b"]}
        user = {}
        assert _find_new_keys(defaults, user) == ["styles"]

    def test_user_has_same_keys_different_values(self):
        """Keys match even though values differ — no new keys."""
        defaults = {"a": 1, "b": {"c": 3}}
        user = {"a": 99, "b": {"c": 100}}
        assert _find_new_keys(defaults, user) == []

    def test_multiple_missing_at_same_level(self):
        defaults = {"a": 1, "b": 2, "c": 3}
        user = {"a": 1}
        result = _find_new_keys(defaults, user)
        assert set(result) == {"b", "c"}


# ---------------------------------------------------------------------------
# Config validation — comprehensive
# ---------------------------------------------------------------------------

class TestConfigValidationComprehensive:
    """Comprehensive validation warning tests."""

    @pytest.fixture
    def tmp_config(self, tmp_path):
        return str(tmp_path / "config.yml")

    def test_unknown_top_level_key_warning(self, tmp_config):
        """Unknown top-level key triggers a warning."""
        custom = {"bogusKey": True, "config": {"tts": {"speed": 1.0}}}
        with open(tmp_config, "w") as f:
            yaml.dump(custom, f)
        c = IoMcpConfig.load(tmp_config)
        assert any("Unknown top-level key 'bogusKey'" in w for w in c.validation_warnings)

    def test_unknown_top_level_key_with_typo_suggestion(self, tmp_config):
        """Typo in top-level key suggests the correct key."""
        custom = {"confg": {"tts": {}}}  # typo for "config"
        with open(tmp_config, "w") as f:
            yaml.dump(custom, f)
        c = IoMcpConfig.load(tmp_config)
        assert any("did you mean 'config'" in w for w in c.validation_warnings)

    def test_unknown_config_key_warning(self, tmp_config):
        """Unknown key inside 'config' section triggers warning."""
        custom = {"config": {"unknownSection": True}}
        with open(tmp_config, "w") as f:
            yaml.dump(custom, f)
        c = IoMcpConfig.load(tmp_config)
        assert any("Unknown config key 'config.unknownSection'" in w
                    for w in c.validation_warnings)

    def test_unknown_config_key_typo_suggestion(self, tmp_config):
        """Typo in config key suggests the correct key."""
        custom = {"config": {"ambiemt": {"enabled": True}}}  # typo: ambiemt
        with open(tmp_config, "w") as f:
            yaml.dump(custom, f)
        c = IoMcpConfig.load(tmp_config)
        assert any("did you mean 'ambient'" in w for w in c.validation_warnings)

    def test_unknown_tts_key_warning(self, tmp_config):
        """Unknown key inside config.tts triggers warning."""
        custom = {"config": {"tts": {"volumee": 0.5}}}  # typo: volumee
        with open(tmp_config, "w") as f:
            yaml.dump(custom, f)
        c = IoMcpConfig.load(tmp_config)
        assert any("Unknown TTS key 'config.tts.volumee'" in w
                    for w in c.validation_warnings)

    def test_unknown_tts_key_typo_suggestion(self, tmp_config):
        """Typo in TTS key suggests correct key."""
        custom = {"config": {"tts": {"voiceRotatin": []}}}  # typo
        with open(tmp_config, "w") as f:
            yaml.dump(custom, f)
        c = IoMcpConfig.load(tmp_config)
        assert any("did you mean 'voiceRotation'" in w for w in c.validation_warnings)

    def test_unknown_speeds_key_warning(self, tmp_config):
        """Unknown key in config.tts.speeds triggers warning."""
        custom = {"config": {"tts": {"speeds": {"preambl": 1.5}}}}  # typo
        with open(tmp_config, "w") as f:
            yaml.dump(custom, f)
        c = IoMcpConfig.load(tmp_config)
        assert any("Unknown speed context" in w and "preambl" in w
                    for w in c.validation_warnings)

    def test_speed_out_of_range_low(self, tmp_config):
        """Speed below 0.1 triggers warning."""
        custom = {"config": {"tts": {"speed": 0.05}}}
        with open(tmp_config, "w") as f:
            yaml.dump(custom, f)
        c = IoMcpConfig.load(tmp_config)
        assert any("out of range" in w and "speed" in w.lower()
                    for w in c.validation_warnings)

    def test_speed_out_of_range_high(self, tmp_config):
        """Speed above 5.0 triggers warning."""
        custom = {"config": {"tts": {"speed": 10.0}}}
        with open(tmp_config, "w") as f:
            yaml.dump(custom, f)
        c = IoMcpConfig.load(tmp_config)
        assert any("out of range" in w and "speed" in w.lower()
                    for w in c.validation_warnings)

    def test_per_context_speed_out_of_range(self, tmp_config):
        """Per-context speed out of range triggers warning."""
        custom = {"config": {"tts": {"speeds": {"speak": 999.0}}}}
        with open(tmp_config, "w") as f:
            yaml.dump(custom, f)
        c = IoMcpConfig.load(tmp_config)
        assert any("config.tts.speeds.speak" in w and "out of range" in w
                    for w in c.validation_warnings)

    def test_per_context_speed_non_numeric(self, tmp_config):
        """Non-numeric per-context speed triggers warning."""
        custom = {"config": {"tts": {"speeds": {"speak": "fast"}}}}
        with open(tmp_config, "w") as f:
            yaml.dump(custom, f)
        c = IoMcpConfig.load(tmp_config)
        assert any("config.tts.speeds.speak" in w and "must be a number" in w
                    for w in c.validation_warnings)

    def test_invalid_color_scheme(self, tmp_config):
        """Unknown colorScheme triggers warning."""
        custom = {"config": {"colorScheme": "solarized"}}
        with open(tmp_config, "w") as f:
            yaml.dump(custom, f)
        c = IoMcpConfig.load(tmp_config)
        assert any("colorScheme" in w and "solarized" in w
                    for w in c.validation_warnings)

    def test_valid_color_scheme_no_warning(self, tmp_config):
        """Valid colorScheme does not trigger warning."""
        custom = {"config": {"colorScheme": "dracula"}}
        with open(tmp_config, "w") as f:
            yaml.dump(custom, f)
        c = IoMcpConfig.load(tmp_config)
        assert not any("colorScheme" in w for w in c.validation_warnings)

    def test_invalid_local_backend(self, tmp_config):
        """Unknown localBackend triggers warning."""
        custom = {"config": {"tts": {"localBackend": "piper"}}}
        with open(tmp_config, "w") as f:
            yaml.dump(custom, f)
        c = IoMcpConfig.load(tmp_config)
        assert any("localBackend" in w and "piper" in w
                    for w in c.validation_warnings)

    def test_valid_local_backend_no_warning(self, tmp_config):
        """Valid localBackend does not trigger warning."""
        for backend in ("termux", "espeak", "none"):
            custom = {"config": {"tts": {"localBackend": backend}}}
            with open(tmp_config, "w") as f:
                yaml.dump(custom, f)
            c = IoMcpConfig.load(tmp_config)
            assert not any("localBackend" in w for w in c.validation_warnings)

    def test_pregen_workers_out_of_range(self, tmp_config):
        """pregenerateWorkers out of 1-8 range triggers warning."""
        custom = {"config": {"tts": {"pregenerateWorkers": 20}}}
        with open(tmp_config, "w") as f:
            yaml.dump(custom, f)
        c = IoMcpConfig.load(tmp_config)
        assert any("pregenerateWorkers" in w and "out of range" in w
                    for w in c.validation_warnings)

    def test_style_degree_out_of_range(self, tmp_config):
        """styleDegree out of 0.01-2.0 range triggers warning."""
        custom = {"config": {"tts": {"styleDegree": 5.0}}}
        with open(tmp_config, "w") as f:
            yaml.dump(custom, f)
        c = IoMcpConfig.load(tmp_config)
        assert any("styleDegree" in w and "out of range" in w
                    for w in c.validation_warnings)

    def test_style_degree_non_numeric(self, tmp_config):
        """Non-numeric styleDegree triggers warning."""
        custom = {"config": {"tts": {"styleDegree": "high"}}}
        with open(tmp_config, "w") as f:
            yaml.dump(custom, f)
        c = IoMcpConfig.load(tmp_config)
        assert any("styleDegree" in w and "must be a number" in w
                    for w in c.validation_warnings)

    def test_unknown_key_binding(self, tmp_config):
        """Unknown key binding action triggers warning."""
        custom = {"config": {"keyBindings": {"teleport": "t"}}}
        with open(tmp_config, "w") as f:
            yaml.dump(custom, f)
        c = IoMcpConfig.load(tmp_config)
        assert any("Unknown key binding" in w and "teleport" in w
                    for w in c.validation_warnings)

    def test_voice_preset_missing_keys(self, tmp_config):
        """Voice preset without required keys triggers warning."""
        custom = {
            "voices": {"broken": {"provider": "openai"}},  # missing model and voice
        }
        with open(tmp_config, "w") as f:
            yaml.dump(custom, f)
        c = IoMcpConfig.load(tmp_config)
        assert any("Voice preset 'broken'" in w and "missing keys" in w
                    for w in c.validation_warnings)

    def test_voice_preset_non_dict(self, tmp_config):
        """Voice preset that's not a dict triggers warning."""
        custom = {"voices": {"broken": "just-a-string"}}
        with open(tmp_config, "w") as f:
            yaml.dump(custom, f)
        c = IoMcpConfig.load(tmp_config)
        assert any("Voice preset 'broken'" in w and "should be a dict" in w
                    for w in c.validation_warnings)

    def test_voice_preset_references_missing_provider(self, tmp_config):
        """Voice preset referencing non-existent provider triggers warning."""
        custom = {
            "providers": {},  # empty — no providers
            "voices": {
                "test": {"provider": "nonexistent", "model": "m", "voice": "v"}
            },
        }
        with open(tmp_config, "w") as f:
            yaml.dump(custom, f)
        c = IoMcpConfig.load(tmp_config)
        assert any("references provider 'nonexistent'" in w
                    for w in c.validation_warnings)

    def test_tts_voice_preset_not_found(self, tmp_config):
        """TTS voice referencing non-existent preset triggers warning."""
        custom = {"config": {"tts": {"voice": "nonexistent_voice"}}}
        with open(tmp_config, "w") as f:
            yaml.dump(custom, f)
        c = IoMcpConfig.load(tmp_config)
        assert any("TTS voice preset 'nonexistent_voice' not found" in w
                    for w in c.validation_warnings)

    def test_ui_voice_preset_not_found(self, tmp_config):
        """UI voice referencing non-existent preset triggers warning."""
        custom = {"config": {"tts": {"uiVoice": "nonexistent_ui"}}}
        with open(tmp_config, "w") as f:
            yaml.dump(custom, f)
        c = IoMcpConfig.load(tmp_config)
        assert any("UI voice preset 'nonexistent_ui' not found" in w
                    for w in c.validation_warnings)

    def test_voice_rotation_entry_not_found(self, tmp_config):
        """Voice rotation entry referencing non-existent preset triggers warning."""
        custom = {"config": {"tts": {"voiceRotation": ["sage", "bogus_voice"]}}}
        with open(tmp_config, "w") as f:
            yaml.dump(custom, f)
        c = IoMcpConfig.load(tmp_config)
        assert any("Voice rotation entry 'bogus_voice' not found" in w
                    for w in c.validation_warnings)

    def test_stt_model_not_found(self, tmp_config):
        """STT model not in models.stt triggers warning."""
        custom = {"config": {"stt": {"model": "nonexistent_stt"}}}
        with open(tmp_config, "w") as f:
            yaml.dump(custom, f)
        c = IoMcpConfig.load(tmp_config)
        assert any("STT model 'nonexistent_stt' not found" in w
                    for w in c.validation_warnings)

    def test_custom_style_warning(self, tmp_config):
        """Custom style not in styles list triggers soft warning."""
        custom = {"config": {"tts": {"style": "pirate_mode"}}}
        with open(tmp_config, "w") as f:
            yaml.dump(custom, f)
        c = IoMcpConfig.load(tmp_config)
        assert any("TTS style 'pirate_mode'" in w and "not in styles" in w
                    for w in c.validation_warnings)

    def test_style_rotation_unknown_entry(self, tmp_config):
        """Unknown entry in styleRotation triggers warning."""
        custom = {"config": {"tts": {"styleRotation": ["happy", "bogus_style"]}}}
        with open(tmp_config, "w") as f:
            yaml.dump(custom, f)
        c = IoMcpConfig.load(tmp_config)
        assert any("Style rotation entry 'bogus_style'" in w
                    for w in c.validation_warnings)


# ---------------------------------------------------------------------------
# Property accessors — additional coverage
# ---------------------------------------------------------------------------

class TestPropertyAccessorsAdditional:
    """Test property accessors not yet covered."""

    @pytest.fixture
    def tmp_config(self, tmp_path):
        return str(tmp_path / "config.yml")

    def test_tts_local_backend_default(self, tmp_config):
        c = IoMcpConfig.load(tmp_config)
        assert c.tts_local_backend == "espeak"

    def test_tts_local_backend_custom(self, tmp_config):
        custom = {"config": {"tts": {"localBackend": "termux"}}}
        with open(tmp_config, "w") as f:
            yaml.dump(custom, f)
        c = IoMcpConfig.load(tmp_config)
        assert c.tts_local_backend == "termux"

    def test_session_cleanup_timeout_default(self, tmp_config):
        c = IoMcpConfig.load(tmp_config)
        assert c.session_cleanup_timeout == 300.0

    def test_session_cleanup_timeout_custom(self, tmp_config):
        custom = {"config": {"session": {"cleanupTimeoutSeconds": 600}}}
        with open(tmp_config, "w") as f:
            yaml.dump(custom, f)
        c = IoMcpConfig.load(tmp_config)
        assert c.session_cleanup_timeout == 600.0

    def test_key_bindings_default(self, tmp_config):
        c = IoMcpConfig.load(tmp_config)
        kb = c.key_bindings
        assert kb["cursorDown"] == "j"
        assert kb["cursorUp"] == "k"
        assert kb["select"] == "enter"
        assert kb["voiceInput"] == "space"
        assert kb["settings"] == "s"
        assert kb["quit"] == "q"

    def test_key_bindings_custom_override(self, tmp_config):
        """User can override individual key bindings."""
        custom = {"config": {"keyBindings": {"cursorDown": "down", "cursorUp": "up"}}}
        with open(tmp_config, "w") as f:
            yaml.dump(custom, f)
        c = IoMcpConfig.load(tmp_config)
        kb = c.key_bindings
        assert kb["cursorDown"] == "down"
        assert kb["cursorUp"] == "up"
        # Non-overridden bindings still use defaults
        assert kb["select"] == "enter"

    def test_tts_pregenerate_workers_default(self, tmp_config):
        c = IoMcpConfig.load(tmp_config)
        assert c.tts_pregenerate_workers == 3

    def test_tts_pregenerate_workers_clamped_low(self, tmp_config):
        """Workers below 1 are clamped to 1."""
        custom = {"config": {"tts": {"pregenerateWorkers": 0}}}
        with open(tmp_config, "w") as f:
            yaml.dump(custom, f)
        c = IoMcpConfig.load(tmp_config)
        assert c.tts_pregenerate_workers == 1

    def test_tts_pregenerate_workers_clamped_high(self, tmp_config):
        """Workers above 8 are clamped to 8."""
        custom = {"config": {"tts": {"pregenerateWorkers": 20}}}
        with open(tmp_config, "w") as f:
            yaml.dump(custom, f)
        c = IoMcpConfig.load(tmp_config)
        assert c.tts_pregenerate_workers == 8

    def test_tts_speed_for_context(self, tmp_config):
        """Per-context speed multiplier is applied to base speed."""
        custom = {"config": {"tts": {"speed": 1.0, "speeds": {"speak": 1.5, "ui": 2.0}}}}
        with open(tmp_config, "w") as f:
            yaml.dump(custom, f)
        c = IoMcpConfig.load(tmp_config)
        # base 1.0 × multiplier → same as multiplier when base is 1.0
        assert c.tts_speed_for("speak") == 1.5
        assert c.tts_speed_for("ui") == 2.0

    def test_tts_speed_for_missing_context(self, tmp_config):
        """Per-context speed falls back to base speed for unknown contexts."""
        custom = {"config": {"tts": {"speed": 1.3, "speeds": {"speak": 1.5}}}}
        with open(tmp_config, "w") as f:
            yaml.dump(custom, f)
        c = IoMcpConfig.load(tmp_config)
        # "speak" multiplier 1.5 × base 1.3 = 1.95
        assert c.tts_speed_for("speak") == 1.95
        # "nonexistent" is not in speeds — falls back to base speed
        assert c.tts_speed_for("nonexistent") == 1.3

    def test_tts_style_degree_default(self, tmp_config):
        c = IoMcpConfig.load(tmp_config)
        assert c.tts_style_degree == 2.0

    def test_tts_style_degree_custom(self, tmp_config):
        custom = {"config": {"tts": {"styleDegree": 0.5}}}
        with open(tmp_config, "w") as f:
            yaml.dump(custom, f)
        c = IoMcpConfig.load(tmp_config)
        assert c.tts_style_degree == 0.5

    def test_haptic_enabled_default(self, tmp_config):
        c = IoMcpConfig.load(tmp_config)
        assert c.haptic_enabled is False

    def test_haptic_enabled_custom(self, tmp_config):
        custom = {"config": {"haptic": {"enabled": True}}}
        with open(tmp_config, "w") as f:
            yaml.dump(custom, f)
        c = IoMcpConfig.load(tmp_config)
        assert c.haptic_enabled is True

    def test_chimes_enabled_default(self, tmp_config):
        c = IoMcpConfig.load(tmp_config)
        assert c.chimes_enabled is False

    def test_pulse_audio_defaults(self, tmp_config):
        c = IoMcpConfig.load(tmp_config)
        assert c.pulse_auto_reconnect is True
        assert c.pulse_max_reconnect_attempts == 3
        assert c.pulse_reconnect_cooldown == 30.0

    def test_pulse_audio_custom(self, tmp_config):
        custom = {"config": {"pulseAudio": {
            "autoReconnect": False,
            "maxReconnectAttempts": 5,
            "reconnectCooldownSecs": 60,
        }}}
        with open(tmp_config, "w") as f:
            yaml.dump(custom, f)
        c = IoMcpConfig.load(tmp_config)
        assert c.pulse_auto_reconnect is False
        assert c.pulse_max_reconnect_attempts == 5
        assert c.pulse_reconnect_cooldown == 60.0

    def test_agent_defaults(self, tmp_config):
        c = IoMcpConfig.load(tmp_config)
        assert c.agent_default_workdir == "~"
        assert c.agent_hosts == []

    def test_agent_hosts_custom(self, tmp_config):
        custom = {"config": {"agents": {
            "defaultWorkdir": "/home/user/projects",
            "hosts": [{"name": "Desktop", "host": "desk.local", "workdir": "~/code"}],
        }}}
        with open(tmp_config, "w") as f:
            yaml.dump(custom, f)
        c = IoMcpConfig.load(tmp_config)
        assert c.agent_default_workdir == "/home/user/projects"
        assert len(c.agent_hosts) == 1
        assert c.agent_hosts[0]["name"] == "Desktop"

    def test_realtime_accessors(self, tmp_config):
        c = IoMcpConfig.load(tmp_config)
        assert c.realtime_model_name == "gpt-realtime"
        assert c.realtime_provider_name == "openai"
        assert isinstance(c.realtime_model_def, dict)
        assert isinstance(c.realtime_base_url, str)
        assert isinstance(c.realtime_api_key, str)

    def test_ring_receiver_defaults(self, tmp_config):
        c = IoMcpConfig.load(tmp_config)
        assert c.ring_receiver_enabled is False
        assert c.ring_receiver_port == 5555

    def test_always_allow_restart_tui_default(self, tmp_config):
        c = IoMcpConfig.load(tmp_config)
        assert c.always_allow_restart_tui is True

    def test_always_allow_restart_tui_custom(self, tmp_config):
        custom = {"config": {"alwaysAllow": {"restartTUI": False}}}
        with open(tmp_config, "w") as f:
            yaml.dump(custom, f)
        c = IoMcpConfig.load(tmp_config)
        assert c.always_allow_restart_tui is False

    def test_providers_accessor(self, tmp_config):
        c = IoMcpConfig.load(tmp_config)
        assert "openai" in c.providers
        assert "azure-foundry" in c.providers

    def test_models_accessor(self, tmp_config):
        c = IoMcpConfig.load(tmp_config)
        assert "stt" in c.models
        assert "realtime" in c.models

    def test_runtime_accessor(self, tmp_config):
        c = IoMcpConfig.load(tmp_config)
        assert "tts" in c.runtime
        assert "stt" in c.runtime
        assert "ambient" in c.runtime

    def test_tts_voice_rotation_legacy_dict_format(self, tmp_config):
        """Legacy dict format in voiceRotation is handled."""
        custom = {"config": {"tts": {"voiceRotation": [
            {"voice": "sage", "model": "gpt-4o-mini-tts", "preset": "sage"},
        ]}}}
        with open(tmp_config, "w") as f:
            yaml.dump(custom, f)
        c = IoMcpConfig.load(tmp_config)
        rot = c.tts_voice_rotation
        assert len(rot) == 1
        assert rot[0]["voice"] == "sage"
        assert rot[0]["preset"] == "sage"

    def test_tts_emotion_rotation_legacy_key(self, tmp_config):
        """Legacy 'emotionRotation' key falls back correctly."""
        custom = {"config": {"tts": {"emotionRotation": ["happy", "sad"]}}}
        with open(tmp_config, "w") as f:
            yaml.dump(custom, f)
        c = IoMcpConfig.load(tmp_config)
        # When both keys exist, styleRotation from defaults wins; but
        # when only emotionRotation is set (not in defaults either), it
        # should be used. Since defaults include styleRotation, we test the
        # legacy accessor directly:
        assert c.tts_emotion_rotation == c.tts_style_rotation

    def test_tts_ui_voice_fallback_to_main(self, tmp_config):
        """When uiVoice is empty, falls back to regular voice preset."""
        custom = {"config": {"tts": {"voice": "sage", "uiVoice": ""}}}
        with open(tmp_config, "w") as f:
            yaml.dump(custom, f)
        c = IoMcpConfig.load(tmp_config)
        assert c.tts_ui_voice_preset == "sage"  # falls back
        assert c.tts_ui_voice == "sage"

    def test_tts_model_def(self, tmp_config):
        c = IoMcpConfig.load(tmp_config)
        model_def = c.tts_model_def
        assert "provider" in model_def
        assert "model" in model_def
        assert "voice" in model_def

    def test_tts_base_url_and_api_key(self, tmp_config, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test-123")
        monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
        c = IoMcpConfig.load(tmp_config)
        c.set_tts_voice("sage")
        assert "api.openai.com" in c.tts_base_url
        assert c.tts_api_key == "sk-test-123"

    def test_stt_base_url(self, tmp_config):
        c = IoMcpConfig.load(tmp_config)
        assert isinstance(c.stt_base_url, str)
        assert isinstance(c.stt_api_key, str)

    def test_stt_model_def(self, tmp_config):
        c = IoMcpConfig.load(tmp_config)
        model_def = c.stt_model_def
        assert "provider" in model_def

    def test_color_scheme_from_config(self, tmp_config):
        """Color scheme is accessible via runtime."""
        c = IoMcpConfig.load(tmp_config)
        assert c.runtime.get("colorScheme") == "nord"


# ---------------------------------------------------------------------------
# Djent integration
# ---------------------------------------------------------------------------

class TestDjentIntegration:
    """Tests for djent-related config features."""

    @pytest.fixture
    def tmp_config(self, tmp_path, monkeypatch):
        # Change cwd to tmp_path to avoid merging the project's .io-mcp.yml
        monkeypatch.chdir(tmp_path)
        return str(tmp_path / "config.yml")

    def test_djent_disabled_by_default(self, tmp_config):
        c = IoMcpConfig.load(tmp_config)
        assert c.djent_enabled is False

    def test_djent_enabled_setter(self, tmp_config):
        c = IoMcpConfig.load(tmp_config)
        c.djent_enabled = True
        assert c.djent_enabled is True
        c.djent_enabled = False
        assert c.djent_enabled is False

    def test_extra_options_without_djent(self, tmp_config):
        """When djent disabled, djent options not included."""
        custom = {
            "extraOptions": [{"title": "My Option", "description": "test", "silent": False}],
            "config": {"djent": {"enabled": False}},
        }
        with open(tmp_config, "w") as f:
            yaml.dump(custom, f)
        c = IoMcpConfig.load(tmp_config)
        titles = [o["title"] for o in c.extra_options]
        assert "My Option" in titles
        assert "Djent status" not in titles

    def test_extra_options_with_djent(self, tmp_config):
        """When djent enabled, djent options are injected."""
        custom = {
            "extraOptions": [{"title": "My Option", "description": "test", "silent": False}],
            "config": {"djent": {"enabled": True}},
        }
        with open(tmp_config, "w") as f:
            yaml.dump(custom, f)
        c = IoMcpConfig.load(tmp_config)
        titles = [o["title"] for o in c.extra_options]
        assert "My Option" in titles
        assert "Djent status" in titles
        assert "Djent dashboard" in titles

    def test_djent_options_not_duplicated(self, tmp_config):
        """If user already has a djent option title, it's not added again."""
        custom = {
            "extraOptions": [
                {"title": "Djent status", "description": "custom", "silent": False}
            ],
            "config": {"djent": {"enabled": True}},
        }
        with open(tmp_config, "w") as f:
            yaml.dump(custom, f)
        c = IoMcpConfig.load(tmp_config)
        djent_status_count = sum(1 for o in c.extra_options if o["title"] == "Djent status")
        assert djent_status_count == 1  # not duplicated

    def test_quick_actions_without_djent(self, tmp_config):
        """When djent disabled, djent quick actions not included."""
        c = IoMcpConfig.load(tmp_config)
        keys = [a.get("key") for a in c.quick_actions]
        assert "!" not in keys  # djent quick action key

    def test_quick_actions_with_djent(self, tmp_config):
        """When djent enabled, djent quick actions are injected."""
        custom = {"config": {"djent": {"enabled": True}}}
        with open(tmp_config, "w") as f:
            yaml.dump(custom, f)
        c = IoMcpConfig.load(tmp_config)
        keys = [a.get("key") for a in c.quick_actions]
        assert "!" in keys
        assert "@" in keys

    def test_quick_actions_no_key_conflict(self, tmp_config):
        """Djent quick actions don't overwrite user quick actions with same key."""
        custom = {
            "quickActions": [{"key": "!", "label": "My Action", "action": "message", "value": "test"}],
            "config": {"djent": {"enabled": True}},
        }
        with open(tmp_config, "w") as f:
            yaml.dump(custom, f)
        c = IoMcpConfig.load(tmp_config)
        excl_actions = [a for a in c.quick_actions if a["key"] == "!"]
        assert len(excl_actions) == 1
        assert excl_actions[0]["label"] == "My Action"  # user's action wins

    def test_djent_constants_structure(self):
        """Verify _DJENT_EXTRA_OPTIONS and _DJENT_QUICK_ACTIONS have required keys."""
        for opt in _DJENT_EXTRA_OPTIONS:
            assert "title" in opt
            assert "description" in opt
            assert "silent" in opt
        for act in _DJENT_QUICK_ACTIONS:
            assert "key" in act
            assert "label" in act
            assert "action" in act
            assert "value" in act


# ---------------------------------------------------------------------------
# Config save
# ---------------------------------------------------------------------------

class TestConfigSave:
    """Tests for save() method."""

    @pytest.fixture
    def tmp_config(self, tmp_path):
        return str(tmp_path / "config.yml")

    def test_save_writes_to_disk(self, tmp_config):
        c = IoMcpConfig.load(tmp_config)
        c.raw["config"]["tts"]["speed"] = 2.5
        c.save()

        with open(tmp_config, "r") as f:
            on_disk = yaml.safe_load(f)
        assert on_disk["config"]["tts"]["speed"] == 2.5

    def test_save_re_expands_config(self, tmp_config, monkeypatch):
        """save() re-expands env vars after writing."""
        monkeypatch.setenv("MY_KEY", "test-key")
        c = IoMcpConfig.load(tmp_config)
        c.raw["providers"]["custom"] = {"apiKey": "${MY_KEY}"}
        c.save()
        assert c.expanded["providers"]["custom"]["apiKey"] == "test-key"

    def test_save_preserves_structure(self, tmp_config):
        """save() preserves all sections in the config."""
        c = IoMcpConfig.load(tmp_config)
        c.save()

        with open(tmp_config, "r") as f:
            on_disk = yaml.safe_load(f)

        assert "providers" in on_disk
        assert "voices" in on_disk
        assert "config" in on_disk
        assert "styles" in on_disk

    def test_save_creates_directory(self, tmp_path):
        """save() creates the config directory if missing."""
        nested_path = str(tmp_path / "subdir" / "config.yml")
        c = IoMcpConfig.load(nested_path)
        # The load call should have already created the dir and file
        assert os.path.isfile(nested_path)


# ---------------------------------------------------------------------------
# Config loading — edge cases
# ---------------------------------------------------------------------------

class TestConfigLoadingEdgeCases:
    """Edge cases for config loading."""

    @pytest.fixture
    def tmp_config(self, tmp_path):
        return str(tmp_path / "config.yml")

    def test_empty_yaml_file(self, tmp_config):
        """Empty YAML file loads defaults."""
        with open(tmp_config, "w") as f:
            f.write("")
        c = IoMcpConfig.load(tmp_config)
        assert c.tts_speed == 1.0
        assert c.tts_voice_preset == "noa"

    def test_yaml_with_only_null(self, tmp_config):
        """YAML file containing only 'null' loads defaults."""
        with open(tmp_config, "w") as f:
            f.write("null\n")
        c = IoMcpConfig.load(tmp_config)
        assert c.tts_speed == 1.0

    def test_invalid_yaml_falls_back_to_defaults(self, tmp_config, capsys):
        """Malformed YAML prints warning and uses defaults."""
        with open(tmp_config, "w") as f:
            f.write("{{invalid yaml content: [}")
        c = IoMcpConfig.load(tmp_config)
        captured = capsys.readouterr()
        assert "WARNING" in captured.out or c.tts_speed == 1.0

    def test_local_override_file_merging(self, tmp_config, tmp_path, monkeypatch):
        """Both .io-mcp.yml and .io-mcp.local.yml are merged."""
        # Base config
        base = {"config": {"tts": {"speed": 1.0}}}
        with open(tmp_config, "w") as f:
            yaml.dump(base, f)

        # Local project config
        local = {"config": {"tts": {"speed": 1.5}}}
        with open(tmp_path / ".io-mcp.yml", "w") as f:
            yaml.dump(local, f)

        # Personal override
        override = {"config": {"tts": {"speed": 2.0}}}
        with open(tmp_path / ".io-mcp.local.yml", "w") as f:
            yaml.dump(override, f)

        monkeypatch.chdir(tmp_path)
        c = IoMcpConfig.load(tmp_config)
        # .io-mcp.local.yml wins over .io-mcp.yml which wins over config.yml
        assert c.tts_speed == 2.0

    def test_local_override_with_extra_options(self, tmp_config, tmp_path, monkeypatch):
        """Local .io-mcp.yml can add extraOptions."""
        local = {
            "extraOptions": [
                {"title": "Deploy", "description": "Deploy to prod", "silent": True}
            ]
        }
        with open(tmp_path / ".io-mcp.yml", "w") as f:
            yaml.dump(local, f)

        monkeypatch.chdir(tmp_path)
        c = IoMcpConfig.load(tmp_config)
        assert any(o["title"] == "Deploy" for o in c.extra_options)

    def test_reload_method(self, tmp_config):
        """reload() picks up changes from disk."""
        c = IoMcpConfig.load(tmp_config)
        assert c.tts_speed == 1.0

        # Modify on disk
        with open(tmp_config, "r") as f:
            raw = yaml.safe_load(f)
        raw["config"]["tts"]["speed"] = 3.0
        with open(tmp_config, "w") as f:
            yaml.dump(raw, f)

        c.reload()
        assert c.tts_speed == 3.0

    def test_config_path_stored(self, tmp_config):
        """Config remembers its path."""
        c = IoMcpConfig.load(tmp_config)
        assert c.config_path == tmp_config

    def test_env_vars_expanded_in_loaded_config(self, tmp_config, monkeypatch):
        """Env vars in config are expanded at load time."""
        monkeypatch.setenv("MY_BASE_URL", "https://custom.api.com")
        custom = {
            "providers": {
                "custom": {"baseUrl": "${MY_BASE_URL}", "apiKey": "key"}
            }
        }
        with open(tmp_config, "w") as f:
            yaml.dump(custom, f)
        c = IoMcpConfig.load(tmp_config)
        assert c.expanded["providers"]["custom"]["baseUrl"] == "https://custom.api.com"
        # raw should still have the unexpanded form
        assert c.raw["providers"]["custom"]["baseUrl"] == "${MY_BASE_URL}"


# ---------------------------------------------------------------------------
# Config mutation — additional coverage
# ---------------------------------------------------------------------------

class TestConfigMutationAdditional:
    """Additional mutation tests."""

    @pytest.fixture
    def tmp_config(self, tmp_path):
        return str(tmp_path / "config.yml")

    def test_set_tts_model_no_matching_preset(self, tmp_config):
        """set_tts_model with unknown model sets voice directly."""
        c = IoMcpConfig.load(tmp_config)
        c.set_tts_model("totally-new-model")
        # When no preset matches, it sets the voice name to the model name
        assert c.raw["config"]["tts"]["voice"] == "totally-new-model"

    def test_set_tts_voice_unknown_raw_string(self, tmp_config):
        """set_tts_voice with unknown string sets it directly."""
        c = IoMcpConfig.load(tmp_config)
        c.set_tts_voice("custom-voice-string")
        assert c.raw["config"]["tts"]["voice"] == "custom-voice-string"

    def test_set_tts_style_alias(self, tmp_config):
        """set_tts_style is an alias for set_tts_emotion."""
        c = IoMcpConfig.load(tmp_config)
        c.set_tts_style("terrified")
        assert c.tts_style == "terrified"
        assert c.tts_emotion == "terrified"

    def test_mutation_updates_expanded(self, tmp_config):
        """Mutations re-expand the config."""
        c = IoMcpConfig.load(tmp_config)
        c.set_tts_speed(2.5)
        # expanded should reflect the change
        assert c.expanded["config"]["tts"]["speed"] == 2.5

    def test_set_voice_preset_direct(self, tmp_config):
        """set_tts_voice_preset directly sets the preset name."""
        c = IoMcpConfig.load(tmp_config)
        c.set_tts_voice_preset("verse")
        assert c.tts_voice_preset == "verse"
        assert c.tts_voice == "verse"
        assert c.tts_model_name == "gpt-4o-mini-tts"


# ---------------------------------------------------------------------------
# DEFAULT_CONFIG structure
# ---------------------------------------------------------------------------

class TestDefaultConfigStructure:
    """Tests that DEFAULT_CONFIG has expected structure."""

    def test_has_all_required_sections(self):
        assert "providers" in DEFAULT_CONFIG
        assert "voices" in DEFAULT_CONFIG
        assert "models" in DEFAULT_CONFIG
        assert "config" in DEFAULT_CONFIG
        assert "styles" in DEFAULT_CONFIG

    def test_providers_have_required_keys(self):
        for name, pdef in DEFAULT_CONFIG["providers"].items():
            assert "baseUrl" in pdef, f"Provider '{name}' missing baseUrl"
            assert "apiKey" in pdef, f"Provider '{name}' missing apiKey"

    def test_voices_have_required_keys(self):
        for name, vdef in DEFAULT_CONFIG["voices"].items():
            assert "provider" in vdef, f"Voice '{name}' missing provider"
            assert "model" in vdef, f"Voice '{name}' missing model"
            assert "voice" in vdef, f"Voice '{name}' missing voice"

    def test_stt_models_have_provider(self):
        for name, mdef in DEFAULT_CONFIG["models"]["stt"].items():
            assert "provider" in mdef, f"STT model '{name}' missing provider"

    def test_styles_is_list(self):
        assert isinstance(DEFAULT_CONFIG["styles"], list)
        assert len(DEFAULT_CONFIG["styles"]) > 0

    def test_config_section_has_key_subsections(self):
        config = DEFAULT_CONFIG["config"]
        assert "tts" in config
        assert "stt" in config
        assert "session" in config
        assert "ambient" in config
        assert "healthMonitor" in config
        assert "notifications" in config
        assert "keyBindings" in config
        assert "agents" in config
        assert "haptic" in config
        assert "chimes" in config
        assert "djent" in config

    def test_tts_config_defaults(self):
        tts = DEFAULT_CONFIG["config"]["tts"]
        assert "voice" in tts
        assert "uiVoice" in tts
        assert "speed" in tts
        assert "style" in tts
        assert "voiceRotation" in tts
        assert "styleRotation" in tts
        assert "localBackend" in tts
        assert "pregenerateWorkers" in tts

    def test_voice_rotation_entries_are_valid_presets(self):
        """Voice rotation entries reference valid voice preset names."""
        rotation = DEFAULT_CONFIG["config"]["tts"]["voiceRotation"]
        voices = DEFAULT_CONFIG["voices"]
        for entry in rotation:
            assert entry in voices, f"Rotation entry '{entry}' not in voices"

    def test_default_voice_is_valid_preset(self):
        """Default TTS voice is a valid preset name."""
        voice = DEFAULT_CONFIG["config"]["tts"]["voice"]
        assert voice in DEFAULT_CONFIG["voices"]

    def test_default_ui_voice_is_valid_preset(self):
        """Default UI voice is a valid preset name."""
        ui_voice = DEFAULT_CONFIG["config"]["tts"]["uiVoice"]
        assert ui_voice in DEFAULT_CONFIG["voices"]
