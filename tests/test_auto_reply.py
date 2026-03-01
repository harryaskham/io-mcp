"""Tests for the conversation mode auto-reply feature.

Verifies:
- _is_continuation_choice correctly identifies single continuation choices
- _is_continuation_choice returns None for non-continuation or multiple matches
- Config parsing for conversation.autoReply and conversation.autoReplyDelaySecs
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
from io_mcp.tui.app import _is_continuation_choice


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


def _choices(*labels: str) -> list[dict]:
    """Build a simple choices list from labels."""
    return [{"label": label, "summary": f"Summary for {label}"} for label in labels]


# ---------------------------------------------------------------------------
# _is_continuation_choice — positive matches
# ---------------------------------------------------------------------------

class TestContinuationChoiceMatches:
    """Single continuation choices should be detected."""

    def test_keep_building(self):
        choices = _choices("Keep building", "Review changes", "Something else")
        assert _is_continuation_choice(choices) == 0

    def test_keep_going(self):
        choices = _choices("Review code", "Keep going")
        assert _is_continuation_choice(choices) == 1

    def test_keep_working(self):
        choices = _choices("Stop", "Keep working")
        assert _is_continuation_choice(choices) == 1

    def test_continue(self):
        choices = _choices("Continue", "Cancel")
        assert _is_continuation_choice(choices) == 0

    def test_continue_building(self):
        """'Continue building' starts with 'continue' — should match."""
        choices = _choices("Abort", "Continue building the feature")
        assert _is_continuation_choice(choices) == 1

    def test_proceed(self):
        choices = _choices("Proceed", "Stop here")
        assert _is_continuation_choice(choices) == 0

    def test_yes_continue(self):
        choices = _choices("No", "Yes, continue")
        assert _is_continuation_choice(choices) == 1

    def test_yes_keep(self):
        choices = _choices("Yes, keep going", "No, stop")
        assert _is_continuation_choice(choices) == 0

    def test_sounds_good(self):
        choices = _choices("Sounds good", "Let me think")
        assert _is_continuation_choice(choices) == 0

    def test_go_ahead(self):
        choices = _choices("Go ahead", "Wait")
        assert _is_continuation_choice(choices) == 0

    def test_do_it(self):
        choices = _choices("Skip", "Do it")
        assert _is_continuation_choice(choices) == 1

    def test_looks_good(self):
        choices = _choices("Looks good", "Needs changes")
        assert _is_continuation_choice(choices) == 0

    def test_lgtm(self):
        choices = _choices("LGTM", "Request changes")
        assert _is_continuation_choice(choices) == 0

    def test_case_insensitive(self):
        """Matching should be case-insensitive."""
        choices = _choices("KEEP BUILDING", "Stop")
        assert _is_continuation_choice(choices) == 0

    def test_mixed_case(self):
        choices = _choices("Stop", "kEeP gOiNg")
        assert _is_continuation_choice(choices) == 1

    def test_prefix_match_with_extra_text(self):
        """Labels that START with a pattern should match."""
        choices = _choices("Keep building the REST API", "Switch to frontend")
        assert _is_continuation_choice(choices) == 0


# ---------------------------------------------------------------------------
# _is_continuation_choice — no match
# ---------------------------------------------------------------------------

class TestContinuationChoiceNoMatch:
    """Non-continuation choices should return None."""

    def test_no_continuation_choices(self):
        choices = _choices("Fix the bug", "Add a test", "Review PR")
        assert _is_continuation_choice(choices) is None

    def test_empty_choices(self):
        assert _is_continuation_choice([]) is None

    def test_single_non_continuation(self):
        choices = _choices("Deploy to production")
        assert _is_continuation_choice(choices) is None

    def test_partial_match_not_prefix(self):
        """'keep' embedded in a word should NOT match."""
        choices = _choices("Shopkeeper duties", "Housekeeper tasks")
        assert _is_continuation_choice(choices) is None

    def test_empty_labels(self):
        choices = [{"label": "", "summary": "nothing"}, {"label": "", "summary": "also nothing"}]
        assert _is_continuation_choice(choices) is None

    def test_missing_labels(self):
        choices = [{"summary": "no label key"}]
        assert _is_continuation_choice(choices) is None


# ---------------------------------------------------------------------------
# _is_continuation_choice — multiple matches → None
# ---------------------------------------------------------------------------

class TestContinuationChoiceMultipleMatches:
    """Multiple continuation choices should return None (ambiguous)."""

    def test_two_continuation_choices(self):
        choices = _choices("Keep building", "Continue")
        assert _is_continuation_choice(choices) is None

    def test_three_continuation_choices(self):
        choices = _choices("Proceed", "Keep going", "Yes, continue")
        assert _is_continuation_choice(choices) is None

    def test_two_keep_variants(self):
        choices = _choices("Keep building", "Keep working", "Stop")
        assert _is_continuation_choice(choices) is None

    def test_continue_and_proceed(self):
        choices = _choices("Continue", "Proceed", "Abort")
        assert _is_continuation_choice(choices) is None


# ---------------------------------------------------------------------------
# Config — defaults
# ---------------------------------------------------------------------------

class TestAutoReplyConfigDefaults:
    """Auto-reply should be disabled by default."""

    def test_default_auto_reply_disabled(self, tmp_config):
        config = _make_config(tmp_config)
        assert config.conversation_auto_reply is False

    def test_default_delay(self, tmp_config):
        config = _make_config(tmp_config)
        assert config.conversation_auto_reply_delay == 3.0

    def test_defaults_in_default_config(self):
        """DEFAULT_CONFIG should contain the conversation section."""
        convo = DEFAULT_CONFIG.get("config", {}).get("conversation", {})
        assert convo.get("autoReply") is False
        assert convo.get("autoReplyDelaySecs") == 3.0


# ---------------------------------------------------------------------------
# Config — enabled
# ---------------------------------------------------------------------------

class TestAutoReplyConfigEnabled:
    """When auto-reply is enabled, config values should be accessible."""

    def test_enabled(self, tmp_config):
        config = _make_config(tmp_config, {
            "config": {
                "conversation": {
                    "autoReply": True,
                    "autoReplyDelaySecs": 5.0,
                },
            },
        })
        assert config.conversation_auto_reply is True
        assert config.conversation_auto_reply_delay == 5.0

    def test_custom_delay(self, tmp_config):
        config = _make_config(tmp_config, {
            "config": {
                "conversation": {
                    "autoReply": True,
                    "autoReplyDelaySecs": 1.5,
                },
            },
        })
        assert config.conversation_auto_reply_delay == 1.5

    def test_delay_clamped_low(self, tmp_config):
        """Delay below 0.5 should be clamped to 0.5."""
        config = _make_config(tmp_config, {
            "config": {
                "conversation": {
                    "autoReply": True,
                    "autoReplyDelaySecs": 0.1,
                },
            },
        })
        assert config.conversation_auto_reply_delay == 0.5

    def test_delay_clamped_high(self, tmp_config):
        """Delay above 30.0 should be clamped to 30.0."""
        config = _make_config(tmp_config, {
            "config": {
                "conversation": {
                    "autoReply": True,
                    "autoReplyDelaySecs": 60.0,
                },
            },
        })
        assert config.conversation_auto_reply_delay == 30.0


# ---------------------------------------------------------------------------
# Config — graceful handling
# ---------------------------------------------------------------------------

class TestAutoReplyConfigGraceful:
    """Config should handle missing/invalid values gracefully."""

    def test_missing_conversation_section(self):
        """Missing conversation section → defaults."""
        raw = copy.deepcopy(DEFAULT_CONFIG)
        raw["config"].pop("conversation", None)
        expanded = _expand_config(raw)
        config = IoMcpConfig(raw=raw, expanded=expanded, config_path="/dev/null")
        assert config.conversation_auto_reply is False
        assert config.conversation_auto_reply_delay == 3.0

    def test_missing_config_section(self):
        """Missing config section entirely → defaults."""
        raw = copy.deepcopy(DEFAULT_CONFIG)
        raw.pop("config", None)
        expanded = _expand_config(raw)
        config = IoMcpConfig(raw=raw, expanded=expanded, config_path="/dev/null")
        assert config.conversation_auto_reply is False
        assert config.conversation_auto_reply_delay == 3.0

    def test_invalid_delay_type(self, tmp_config):
        """Non-numeric delay → default 3.0."""
        config = _make_config(tmp_config, {
            "config": {
                "conversation": {
                    "autoReply": True,
                    "autoReplyDelaySecs": "not-a-number",
                },
            },
        })
        assert config.conversation_auto_reply_delay == 3.0

    def test_auto_reply_is_bool(self, tmp_config):
        """autoReply should be coerced to bool."""
        config = _make_config(tmp_config, {
            "config": {
                "conversation": {
                    "autoReply": 1,
                },
            },
        })
        assert config.conversation_auto_reply is True

    def test_delay_is_float(self, tmp_config):
        """Integer delay should be returned as float."""
        config = _make_config(tmp_config, {
            "config": {
                "conversation": {
                    "autoReply": True,
                    "autoReplyDelaySecs": 5,
                },
            },
        })
        result = config.conversation_auto_reply_delay
        assert isinstance(result, float)
        assert result == 5.0


# ---------------------------------------------------------------------------
# Config — no validation warning for 'conversation' key
# ---------------------------------------------------------------------------

class TestAutoReplyConfigValidation:
    """The 'conversation' key should not produce unknown-key warnings."""

    def test_no_warning_for_conversation_key(self, tmp_config):
        config = _make_config(tmp_config, {
            "config": {
                "conversation": {
                    "autoReply": True,
                    "autoReplyDelaySecs": 3.0,
                },
            },
        })
        # Check that no validation warnings mention 'conversation'
        conversation_warnings = [
            w for w in config.validation_warnings
            if "conversation" in w.lower()
        ]
        assert conversation_warnings == [], f"Unexpected warnings: {conversation_warnings}"
