"""Tests for scroll configuration: debounce and inversion settings.

Verifies that scrollDebounce and invertScroll config fields are read
correctly from config YAML and exposed via property accessors with
correct defaults.
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

class TestScrollDefaults:
    """scroll.debounce and scroll.invert should have sensible defaults."""

    def test_default_scroll_debounce(self, tmp_config):
        config = _make_config(tmp_config)
        assert config.scroll_debounce == 0.15

    def test_default_invert_scroll(self, tmp_config):
        config = _make_config(tmp_config)
        assert config.invert_scroll is False

    def test_defaults_in_default_config(self):
        """The DEFAULT_CONFIG dict itself should contain the scroll section."""
        scroll = DEFAULT_CONFIG.get("config", {}).get("scroll", {})
        assert scroll.get("debounce") == 0.15
        assert scroll.get("invert") is False


# ---------------------------------------------------------------------------
# Custom values from config YAML
# ---------------------------------------------------------------------------

class TestScrollCustomConfig:
    """Custom scroll values from YAML should be read correctly."""

    def test_custom_scroll_debounce(self, tmp_config):
        config = _make_config(tmp_config, {
            "config": {
                "scroll": {
                    "debounce": 0.25,
                },
            },
        })
        assert config.scroll_debounce == 0.25

    def test_custom_invert_scroll_true(self, tmp_config):
        config = _make_config(tmp_config, {
            "config": {
                "scroll": {
                    "invert": True,
                },
            },
        })
        assert config.invert_scroll is True

    def test_custom_both_values(self, tmp_config):
        config = _make_config(tmp_config, {
            "config": {
                "scroll": {
                    "debounce": 0.05,
                    "invert": True,
                },
            },
        })
        assert config.scroll_debounce == 0.05
        assert config.invert_scroll is True

    def test_zero_debounce(self, tmp_config):
        """A debounce of 0 should be allowed (no debouncing)."""
        config = _make_config(tmp_config, {
            "config": {
                "scroll": {
                    "debounce": 0.0,
                },
            },
        })
        assert config.scroll_debounce == 0.0

    def test_partial_scroll_config(self, tmp_config):
        """Setting only one field should leave the other at its default."""
        config = _make_config(tmp_config, {
            "config": {
                "scroll": {
                    "invert": True,
                },
            },
        })
        assert config.invert_scroll is True
        assert config.scroll_debounce == 0.15  # default preserved


# ---------------------------------------------------------------------------
# Property accessor types
# ---------------------------------------------------------------------------

class TestScrollPropertyTypes:
    """Properties should return correct types even with YAML quirks."""

    def test_debounce_is_float(self, tmp_config):
        config = _make_config(tmp_config, {
            "config": {
                "scroll": {
                    "debounce": 1,  # int in YAML
                },
            },
        })
        result = config.scroll_debounce
        assert isinstance(result, float)
        assert result == 1.0

    def test_invert_is_bool(self, tmp_config):
        config = _make_config(tmp_config)
        result = config.invert_scroll
        assert isinstance(result, bool)


# ---------------------------------------------------------------------------
# In-memory config (no file)
# ---------------------------------------------------------------------------

class TestScrollInMemoryConfig:
    """Test scroll config with directly constructed IoMcpConfig objects."""

    def test_from_raw_dict(self):
        raw = copy.deepcopy(DEFAULT_CONFIG)
        raw["config"]["scroll"] = {"debounce": 0.3, "invert": True}
        expanded = _expand_config(raw)
        config = IoMcpConfig(raw=raw, expanded=expanded, config_path="/dev/null")
        assert config.scroll_debounce == 0.3
        assert config.invert_scroll is True

    def test_missing_scroll_section(self):
        """If scroll section is entirely missing, defaults should be returned."""
        raw = copy.deepcopy(DEFAULT_CONFIG)
        raw["config"].pop("scroll", None)
        expanded = _expand_config(raw)
        config = IoMcpConfig(raw=raw, expanded=expanded, config_path="/dev/null")
        assert config.scroll_debounce == 0.15
        assert config.invert_scroll is False
