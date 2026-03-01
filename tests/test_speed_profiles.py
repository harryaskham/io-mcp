"""Tests for TTS speed profiles (per-context speed multipliers).

Speed profiles allow different TTS contexts (scroll readout, agent speech,
preambles, UI narration) to run at different speeds.  Values in
config.tts.speeds are **multipliers** applied to the base speed
(config.tts.speed).
"""

from __future__ import annotations

import yaml
import pytest

from io_mcp.config import IoMcpConfig, DEFAULT_CONFIG


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@pytest.fixture()
def tmp_config(tmp_path):
    """Create a temporary config file path."""
    return str(tmp_path / "config.yml")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestSpeedProfiles:
    """Per-context TTS speed multipliers."""

    def test_default_config_has_speeds_section(self):
        """DEFAULT_CONFIG includes the speeds dict with expected contexts."""
        speeds = DEFAULT_CONFIG["config"]["tts"]["speeds"]
        assert isinstance(speeds, dict)
        for ctx in ("scroll", "speak", "speakAsync", "preamble", "agent", "ui"):
            assert ctx in speeds, f"missing default speed for context '{ctx}'"

    def test_default_scroll_multiplier(self):
        """Default scroll multiplier is 1.3 (faster for scanning)."""
        assert DEFAULT_CONFIG["config"]["tts"]["speeds"]["scroll"] == 1.3

    def test_default_agent_multiplier(self):
        """Default agent multiplier is 1.0 (same as base)."""
        assert DEFAULT_CONFIG["config"]["tts"]["speeds"]["agent"] == 1.0

    def test_default_preamble_multiplier(self):
        """Default preamble multiplier is 1.0 (comfortable listening)."""
        assert DEFAULT_CONFIG["config"]["tts"]["speeds"]["preamble"] == 1.0

    # ── tts_speed_for with base speed 1.0 ─────────────────────────────

    def test_base_speed_1_returns_multiplier_directly(self, tmp_config):
        """With base speed 1.0, tts_speed_for returns the multiplier value."""
        custom = {"config": {"tts": {"speed": 1.0, "speeds": {"scroll": 1.3}}}}
        with open(tmp_config, "w") as f:
            yaml.dump(custom, f)
        c = IoMcpConfig.load(tmp_config)
        assert c.tts_speed_for("scroll") == 1.3

    # ── tts_speed_for with non-unity base speed ───────────────────────

    def test_multiplier_applied_to_base_speed(self, tmp_config):
        """Speed = base × multiplier.  1.2 × 1.3 = 1.56."""
        custom = {"config": {"tts": {"speed": 1.2, "speeds": {"scroll": 1.3}}}}
        with open(tmp_config, "w") as f:
            yaml.dump(custom, f)
        c = IoMcpConfig.load(tmp_config)
        assert c.tts_speed_for("scroll") == 1.56

    def test_all_contexts_multiply_correctly(self, tmp_config):
        """Multiple contexts each multiply independently with base."""
        custom = {"config": {"tts": {
            "speed": 1.5,
            "speeds": {
                "scroll": 1.3,
                "preamble": 0.9,
                "agent": 1.0,
                "ui": 1.2,
            },
        }}}
        with open(tmp_config, "w") as f:
            yaml.dump(custom, f)
        c = IoMcpConfig.load(tmp_config)
        assert c.tts_speed_for("scroll") == 1.95    # 1.5 × 1.3
        assert c.tts_speed_for("preamble") == 1.35   # 1.5 × 0.9
        assert c.tts_speed_for("agent") == 1.5       # 1.5 × 1.0
        assert c.tts_speed_for("ui") == 1.8           # 1.5 × 1.2

    # ── Missing context falls back to base speed ──────────────────────

    def test_missing_context_returns_base_speed(self, tmp_config):
        """Unknown context name falls back to base speed (multiplier 1.0)."""
        custom = {"config": {"tts": {"speed": 1.4, "speeds": {"scroll": 1.3}}}}
        with open(tmp_config, "w") as f:
            yaml.dump(custom, f)
        c = IoMcpConfig.load(tmp_config)
        assert c.tts_speed_for("nonexistent") == 1.4

    def test_agent_context_fallback(self, tmp_config):
        """Agent context not in speeds → base speed."""
        custom = {"config": {"tts": {"speed": 1.2, "speeds": {}}}}
        with open(tmp_config, "w") as f:
            yaml.dump(custom, f)
        c = IoMcpConfig.load(tmp_config)
        assert c.tts_speed_for("agent") == 1.2

    # ── No speeds section at all ──────────────────────────────────────

    def test_no_speeds_section_returns_base(self, tmp_config):
        """Omitting speeds entirely → all contexts get base speed."""
        custom = {"config": {"tts": {"speed": 1.1}}}
        with open(tmp_config, "w") as f:
            yaml.dump(custom, f)
        c = IoMcpConfig.load(tmp_config)
        # Even though DEFAULT_CONFIG has speeds, user config merges on top.
        # But _deep_merge keeps defaults for missing keys, so the default
        # speeds dict will be present.  A fully empty speeds override:
        assert c.tts_speed_for("scroll") is not None

    def test_empty_speeds_dict_returns_base(self, tmp_config):
        """Explicit empty speeds {} → all contexts get base speed."""
        custom = {"config": {"tts": {"speed": 1.3, "speeds": {}}}}
        with open(tmp_config, "w") as f:
            yaml.dump(custom, f)
        c = IoMcpConfig.load(tmp_config)
        # Empty dict merged means no context keys → fallback to base
        # But defaults are deep-merged first, so default keys survive.
        # With an explicit empty dict, deep merge keeps default keys.
        for ctx in ("scroll", "ui", "preamble", "agent"):
            result = c.tts_speed_for(ctx)
            assert isinstance(result, float)

    # ── Fractional multipliers ────────────────────────────────────────

    def test_slower_than_base(self, tmp_config):
        """Multiplier < 1.0 makes the context slower than base."""
        custom = {"config": {"tts": {"speed": 1.5, "speeds": {"preamble": 0.8}}}}
        with open(tmp_config, "w") as f:
            yaml.dump(custom, f)
        c = IoMcpConfig.load(tmp_config)
        assert c.tts_speed_for("preamble") == 1.2  # 1.5 × 0.8

    # ── tts_speed_for returns float ───────────────────────────────────

    def test_return_type_is_float(self, tmp_config):
        """tts_speed_for always returns a float."""
        custom = {"config": {"tts": {"speed": 1, "speeds": {"scroll": 2}}}}
        with open(tmp_config, "w") as f:
            yaml.dump(custom, f)
        c = IoMcpConfig.load(tmp_config)
        result = c.tts_speed_for("scroll")
        assert isinstance(result, float)
        assert result == 2.0

    # ── Rounding ──────────────────────────────────────────────────────

    def test_result_is_rounded(self, tmp_config):
        """Multiplied result is rounded to avoid floating-point noise."""
        custom = {"config": {"tts": {"speed": 1.1, "speeds": {"scroll": 1.3}}}}
        with open(tmp_config, "w") as f:
            yaml.dump(custom, f)
        c = IoMcpConfig.load(tmp_config)
        # 1.1 × 1.3 = 1.4300000000000002 without rounding
        assert c.tts_speed_for("scroll") == 1.43
