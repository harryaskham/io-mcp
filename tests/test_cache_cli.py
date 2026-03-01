"""Tests for the io-mcp cache CLI subcommand.

Covers:
- _collect_warmup_texts returns non-empty, deduplicated list
- Expected strings are present (extras, settings, numbers, themes)
- _run_cache_status works with a mock TTSEngine
- _format_size human-readable formatting
"""

from __future__ import annotations

import os
import unittest.mock as mock

import pytest

from io_mcp.__main__ import (
    _collect_warmup_texts,
    _format_size,
    _run_cache_status,
)


# ─── Helpers ─────────────────────────────────────────────────────────


class FakeConfig:
    """Minimal IoMcpConfig stand-in for cache CLI tests."""

    def __init__(self):
        self.tts_model_name = "gpt-4o-mini-tts"
        self.tts_voice = "sage"
        self.tts_voice_preset = "sage"
        self.tts_speed = 1.3
        self.tts_emotion = "friendly"
        self.tts_local_backend = "none"
        self.tts_ui_voice = ""
        self.tts_ui_voice_preset = ""
        self.chimes_enabled = False
        # Provide preset lists
        self.voice_preset_names = ["sage", "alloy", "noa"]
        self.emotion_preset_names = ["neutral", "friendly", "excited"]
        self.tts_style_options = ["neutral", "friendly", "excited"]
        self.stt_model_names = ["whisper", "gpt-4o-mini-transcribe"]
        self.expanded = {"styles": ["neutral", "friendly", "excited"]}

    def tts_speed_for(self, context: str) -> float:
        return self.tts_speed

    def tts_cli_args(self, text, **kwargs):
        return [text, "--model", self.tts_model_name, "--voice", self.tts_voice,
                "--speed", str(self.tts_speed), "--stdout", "--response-format", "wav"]


# ─── Tests ─────────────────────────────────────────────────────────


class TestCollectWarmupTexts:
    """Test _collect_warmup_texts."""

    def test_returns_non_empty_list(self):
        config = FakeConfig()
        texts = _collect_warmup_texts(config)
        assert isinstance(texts, list)
        assert len(texts) > 0

    def test_no_duplicates(self):
        config = FakeConfig()
        texts = _collect_warmup_texts(config)
        assert len(texts) == len(set(texts)), (
            f"Found duplicates: {[t for t in texts if texts.count(t) > 1]}"
        )

    def test_includes_extra_option_labels(self):
        config = FakeConfig()
        texts = _collect_warmup_texts(config)
        # From PRIMARY_EXTRAS
        assert "Record response" in texts
        # From SECONDARY_EXTRAS
        assert "Queue message" in texts
        assert "Dismiss" in texts
        assert "Pane view" in texts
        # From MORE_OPTIONS_ITEM
        assert "More options \u203a" in texts

    def test_includes_settings_labels(self):
        config = FakeConfig()
        texts = _collect_warmup_texts(config)
        assert "Speed" in texts
        assert "Agent voice" in texts
        assert "UI voice" in texts
        assert "Style" in texts
        assert "STT model" in texts
        assert "Close settings" in texts

    def test_includes_common_ui_phrases(self):
        config = FakeConfig()
        texts = _collect_warmup_texts(config)
        assert "Settings" in texts
        assert "Back to choices" in texts
        assert "Help" in texts
        assert "Connected" in texts

    def test_includes_number_words(self):
        config = FakeConfig()
        texts = _collect_warmup_texts(config)
        for word in ["one", "two", "three", "four", "five",
                     "six", "seven", "eight", "nine"]:
            assert word in texts, f"Missing number word: {word}"

    def test_includes_speed_values(self):
        config = FakeConfig()
        texts = _collect_warmup_texts(config)
        assert "0.5" in texts
        assert "1.0" in texts
        assert "2.5" in texts

    def test_includes_theme_names(self):
        config = FakeConfig()
        texts = _collect_warmup_texts(config)
        assert "nord" in texts
        assert "tokyo-night" in texts
        assert "catppuccin" in texts
        assert "dracula" in texts

    def test_includes_emotions_from_config(self):
        config = FakeConfig()
        texts = _collect_warmup_texts(config)
        assert "friendly" in texts
        assert "excited" in texts

    def test_includes_voices_from_config(self):
        config = FakeConfig()
        texts = _collect_warmup_texts(config)
        assert "sage" in texts
        assert "alloy" in texts
        assert "noa" in texts

    def test_includes_stt_models_from_config(self):
        config = FakeConfig()
        texts = _collect_warmup_texts(config)
        assert "whisper" in texts
        assert "gpt-4o-mini-transcribe" in texts

    def test_all_items_are_strings(self):
        config = FakeConfig()
        texts = _collect_warmup_texts(config)
        for t in texts:
            assert isinstance(t, str), f"Non-string item: {t!r}"


class TestFormatSize:
    """Test _format_size helper."""

    def test_bytes(self):
        assert _format_size(500) == "500 B"

    def test_kilobytes(self):
        assert _format_size(2048) == "2.0 KB"

    def test_megabytes(self):
        assert _format_size(5_242_880) == "5.0 MB"

    def test_zero(self):
        assert _format_size(0) == "0 B"


class TestRunCacheStatus:
    """Test _run_cache_status with mocked TTSEngine."""

    def test_prints_stats(self, capsys):
        """_run_cache_status prints item count and size."""
        with mock.patch("io_mcp.__main__.IoMcpConfig") as MockConfig, \
             mock.patch("io_mcp.__main__.TTSEngine") as MockTTS:
            mock_config = FakeConfig()
            MockConfig.load.return_value = mock_config
            mock_tts = mock.MagicMock()
            mock_tts.cache_stats.return_value = (42, 1_048_576)
            mock_tts._cache = {}
            MockTTS.return_value = mock_tts

            _run_cache_status(verbose=False)

            captured = capsys.readouterr()
            assert "42" in captured.out
            assert "1.0 MB" in captured.out
