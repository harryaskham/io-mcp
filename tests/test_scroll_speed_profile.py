"""Tests for scroll speed profile in TUI scroll readout.

The scroll readout should use the 'scroll' speed context from
config.tts.speeds, not the 'ui' speed context. This ensures
scroll readout can be independently tuned for fast scanning.
"""

import os
import tempfile

import yaml
import pytest

from io_mcp.config import IoMcpConfig, DEFAULT_CONFIG


@pytest.fixture()
def tmp_config(tmp_path):
    return str(tmp_path / "config.yml")


class TestScrollSpeedProfile:
    """Test scroll speed context configuration."""

    def test_scroll_speed_default(self, tmp_config):
        """Default scroll speed multiplier should be 1.3."""
        with open(tmp_config, "w") as f:
            yaml.dump({}, f)
        config = IoMcpConfig.load(tmp_config)
        # Default base speed is 1.0, scroll multiplier is 1.3
        speed = config.tts_speed_for("scroll")
        assert speed == 1.3

    def test_ui_speed_default(self, tmp_config):
        """Default UI speed multiplier should be 1.5."""
        with open(tmp_config, "w") as f:
            yaml.dump({}, f)
        config = IoMcpConfig.load(tmp_config)
        speed = config.tts_speed_for("ui")
        assert speed == 1.5

    def test_scroll_and_ui_speeds_differ(self, tmp_config):
        """Scroll and UI speeds should have different defaults."""
        with open(tmp_config, "w") as f:
            yaml.dump({}, f)
        config = IoMcpConfig.load(tmp_config)
        scroll = config.tts_speed_for("scroll")
        ui = config.tts_speed_for("ui")
        assert scroll != ui, "scroll and ui speeds should differ by default"

    def test_scroll_speed_custom(self, tmp_config):
        """Custom scroll speed should be applied."""
        with open(tmp_config, "w") as f:
            yaml.dump({"config": {"tts": {"speed": 1.0, "speeds": {"scroll": 2.0}}}}, f)
        config = IoMcpConfig.load(tmp_config)
        speed = config.tts_speed_for("scroll")
        assert speed == 2.0

    def test_scroll_speed_with_base(self, tmp_config):
        """Scroll speed should multiply with base speed."""
        with open(tmp_config, "w") as f:
            yaml.dump({"config": {"tts": {"speed": 1.2, "speeds": {"scroll": 1.3}}}}, f)
        config = IoMcpConfig.load(tmp_config)
        speed = config.tts_speed_for("scroll")
        assert speed == 1.56  # 1.2 * 1.3

    def test_choicelabel_speed_default(self, tmp_config):
        """Choice label speed should have its own default."""
        with open(tmp_config, "w") as f:
            yaml.dump({}, f)
        config = IoMcpConfig.load(tmp_config)
        speed = config.tts_speed_for("choiceLabel")
        assert speed == 1.5

    def test_choicesummary_speed_default(self, tmp_config):
        """Choice summary speed should have its own default."""
        with open(tmp_config, "w") as f:
            yaml.dump({}, f)
        config = IoMcpConfig.load(tmp_config)
        speed = config.tts_speed_for("choiceSummary")
        assert speed == 2.0

    def test_all_speed_contexts_have_defaults(self, tmp_config):
        """All defined speed contexts should have defaults in the config."""
        with open(tmp_config, "w") as f:
            yaml.dump({}, f)
        config = IoMcpConfig.load(tmp_config)
        contexts = ["scroll", "speak", "speakAsync", "preamble", "agent",
                     "choiceLabel", "choiceSummary", "ui"]
        for ctx in contexts:
            speed = config.tts_speed_for(ctx)
            assert speed > 0, f"Speed for context '{ctx}' should be positive"
            # All should be at least base speed (multiplier >= 1.0)
            assert speed >= config.tts_speed, f"Speed for '{ctx}' should be >= base speed"
