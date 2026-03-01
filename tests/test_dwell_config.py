"""Tests for dwell-to-select configuration.

Verifies that the dwell section in config YAML is read correctly,
the dwell_duration property returns the right values, and CLI args
take precedence over config.
"""

from __future__ import annotations

import copy

import pytest
import yaml

from io_mcp.config import (
    DEFAULT_CONFIG,
    IoMcpConfig,
    _expand_config,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@pytest.fixture()
def tmp_config(tmp_path):
    """Create a temporary config file path."""
    return str(tmp_path / "config.yml")


def _make_config(tmp_config, overrides: dict | None = None) -> IoMcpConfig:
    """Create a config with optional overrides written to a temp file."""
    if overrides:
        with open(tmp_config, "w") as f:
            yaml.dump(overrides, f)
    return IoMcpConfig.load(tmp_config)


# ---------------------------------------------------------------------------
# Default values
# ---------------------------------------------------------------------------

class TestDwellDefaults:
    """Dwell should be disabled by default."""

    def test_default_dwell_disabled(self, tmp_config):
        config = _make_config(tmp_config)
        assert config.dwell_duration == 0.0

    def test_defaults_in_default_config(self):
        """The DEFAULT_CONFIG dict itself should contain the dwell section."""
        dwell = DEFAULT_CONFIG.get("config", {}).get("dwell", {})
        assert dwell.get("enabled") is False
        assert dwell.get("durationSeconds") == 3.0

    def test_disabled_returns_zero(self, tmp_config):
        """When dwell.enabled is False, duration should be 0.0 regardless of durationSeconds."""
        config = _make_config(tmp_config, {
            "config": {
                "dwell": {
                    "enabled": False,
                    "durationSeconds": 5.0,
                },
            },
        })
        assert config.dwell_duration == 0.0


# ---------------------------------------------------------------------------
# Enabled dwell
# ---------------------------------------------------------------------------

class TestDwellEnabled:
    """When dwell is enabled, the configured duration should be returned."""

    def test_enabled_returns_duration(self, tmp_config):
        config = _make_config(tmp_config, {
            "config": {
                "dwell": {
                    "enabled": True,
                    "durationSeconds": 3.0,
                },
            },
        })
        assert config.dwell_duration == 3.0

    def test_enabled_custom_duration(self, tmp_config):
        config = _make_config(tmp_config, {
            "config": {
                "dwell": {
                    "enabled": True,
                    "durationSeconds": 1.5,
                },
            },
        })
        assert config.dwell_duration == 1.5

    def test_enabled_default_duration(self, tmp_config):
        """If enabled but durationSeconds is missing, should use default 3.0."""
        config = _make_config(tmp_config, {
            "config": {
                "dwell": {
                    "enabled": True,
                },
            },
        })
        assert config.dwell_duration == 3.0

    def test_negative_duration_clamped(self, tmp_config):
        """Negative durations should be clamped to 0.0."""
        config = _make_config(tmp_config, {
            "config": {
                "dwell": {
                    "enabled": True,
                    "durationSeconds": -1.0,
                },
            },
        })
        assert config.dwell_duration == 0.0


# ---------------------------------------------------------------------------
# Graceful handling of missing/invalid config
# ---------------------------------------------------------------------------

class TestDwellGraceful:
    """dwell_duration should handle missing/invalid config gracefully."""

    def test_missing_dwell_section(self):
        """If dwell section is entirely missing, should return 0.0."""
        raw = copy.deepcopy(DEFAULT_CONFIG)
        raw["config"].pop("dwell", None)
        expanded = _expand_config(raw)
        config = IoMcpConfig(raw=raw, expanded=expanded, config_path="/dev/null")
        assert config.dwell_duration == 0.0

    def test_missing_config_section(self):
        """If config section is entirely missing, should return 0.0."""
        raw = copy.deepcopy(DEFAULT_CONFIG)
        raw.pop("config", None)
        expanded = _expand_config(raw)
        config = IoMcpConfig(raw=raw, expanded=expanded, config_path="/dev/null")
        assert config.dwell_duration == 0.0

    def test_invalid_duration_type(self, tmp_config):
        """If durationSeconds is not a number, should return 0.0."""
        config = _make_config(tmp_config, {
            "config": {
                "dwell": {
                    "enabled": True,
                    "durationSeconds": "not-a-number",
                },
            },
        })
        assert config.dwell_duration == 0.0

    def test_duration_is_float(self, tmp_config):
        """Integer durationSeconds should be returned as float."""
        config = _make_config(tmp_config, {
            "config": {
                "dwell": {
                    "enabled": True,
                    "durationSeconds": 5,
                },
            },
        })
        result = config.dwell_duration
        assert isinstance(result, float)
        assert result == 5.0


# ---------------------------------------------------------------------------
# CLI arg override simulation
# ---------------------------------------------------------------------------

class TestDwellCLIOverride:
    """CLI --dwell flag should override config value.

    This tests the logic that will be used in __main__.py:
      dwell_time = args.dwell if args.dwell != 0.0 else config.dwell_duration
    """

    def test_cli_overrides_config(self, tmp_config):
        """Non-zero CLI arg should take precedence over config."""
        config = _make_config(tmp_config, {
            "config": {
                "dwell": {
                    "enabled": True,
                    "durationSeconds": 3.0,
                },
            },
        })
        cli_dwell = 5.0
        dwell_time = cli_dwell if cli_dwell != 0.0 else config.dwell_duration
        assert dwell_time == 5.0

    def test_cli_zero_falls_through_to_config(self, tmp_config):
        """CLI arg of 0.0 (default) should use config value."""
        config = _make_config(tmp_config, {
            "config": {
                "dwell": {
                    "enabled": True,
                    "durationSeconds": 2.5,
                },
            },
        })
        cli_dwell = 0.0
        dwell_time = cli_dwell if cli_dwell != 0.0 else config.dwell_duration
        assert dwell_time == 2.5

    def test_cli_zero_and_config_disabled(self, tmp_config):
        """Both CLI and config disabled should result in 0.0."""
        config = _make_config(tmp_config)
        cli_dwell = 0.0
        dwell_time = cli_dwell if cli_dwell != 0.0 else config.dwell_duration
        assert dwell_time == 0.0
