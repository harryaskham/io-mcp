"""Tests for the io-mcp cache CLI subcommand.

Covers:
- _collect_warmup_texts returns non-empty, deduplicated list
- Expected strings are present (extras, settings, numbers, themes)
- _run_cache_status works with a mock TTSEngine (normal and verbose)
- _run_cache_warmup with dry-run, verbose, separate UI voice, all-cached
- _format_size human-readable formatting (including boundary cases)
- _run_cache_command argument parsing and dispatch
- Edge cases: empty voice/emotion lists, no config file
"""

from __future__ import annotations

import os
import tempfile
import unittest.mock as mock

import pytest

from io_mcp.__main__ import (
    _collect_warmup_texts,
    _format_size,
    _run_cache_status,
    _run_cache_warmup,
    _run_cache_command,
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


class FakeConfigWithUIVoice(FakeConfig):
    """FakeConfig with a separate UI voice set."""

    def __init__(self):
        super().__init__()
        self.tts_ui_voice = "teo"
        self.tts_ui_voice_preset = "teo"


class FakeConfigEmpty(FakeConfig):
    """FakeConfig with empty voice/emotion/stt lists."""

    def __init__(self):
        super().__init__()
        self.voice_preset_names = []
        self.emotion_preset_names = []
        self.stt_model_names = []
        self.expanded = {"styles": []}


# ─── Tests: _collect_warmup_texts ────────────────────────────────────


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

    def test_no_empty_strings(self):
        """All collected strings should be non-empty."""
        config = FakeConfig()
        texts = _collect_warmup_texts(config)
        for t in texts:
            assert len(t) > 0, "Empty string in warmup texts"

    def test_empty_config_lists_still_returns_base_texts(self):
        """Even with empty voice/emotion/stt lists, base texts are present."""
        config = FakeConfigEmpty()
        texts = _collect_warmup_texts(config)
        assert len(texts) > 0
        # Core UI strings should always be present
        assert "Speed" in texts
        assert "one" in texts
        assert "nord" in texts
        assert "Record response" in texts


# ─── Tests: _format_size ─────────────────────────────────────────────


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

    def test_exactly_1024(self):
        """1024 bytes is 1.0 KB."""
        assert _format_size(1024) == "1.0 KB"

    def test_exactly_1mb(self):
        """1048576 bytes is 1.0 MB."""
        assert _format_size(1_048_576) == "1.0 MB"

    def test_one_byte(self):
        assert _format_size(1) == "1 B"

    def test_fractional_kb(self):
        """1536 bytes is 1.5 KB."""
        assert _format_size(1536) == "1.5 KB"

    def test_large_mb(self):
        """100 MB."""
        assert _format_size(104_857_600) == "100.0 MB"

    def test_just_below_kb(self):
        """1023 bytes is still in B range."""
        assert _format_size(1023) == "1023 B"


# ─── Tests: _run_cache_status ────────────────────────────────────────


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

    def test_prints_status_header(self, capsys):
        """Output includes the 'io-mcp cache status' header."""
        with mock.patch("io_mcp.__main__.IoMcpConfig") as MockConfig, \
             mock.patch("io_mcp.__main__.TTSEngine") as MockTTS:
            MockConfig.load.return_value = FakeConfig()
            mock_tts = mock.MagicMock()
            mock_tts.cache_stats.return_value = (0, 0)
            mock_tts._cache = {}
            MockTTS.return_value = mock_tts

            _run_cache_status(verbose=False)

            captured = capsys.readouterr()
            assert "io-mcp cache status" in captured.out
            assert "Items:" in captured.out
            assert "Size:" in captured.out

    def test_verbose_with_entries(self, capsys):
        """Verbose mode shows cache entry details."""
        with mock.patch("io_mcp.__main__.IoMcpConfig") as MockConfig, \
             mock.patch("io_mcp.__main__.TTSEngine") as MockTTS:
            MockConfig.load.return_value = FakeConfig()
            mock_tts = mock.MagicMock()
            mock_tts.cache_stats.return_value = (2, 4096)

            # Create real temp files to simulate cache entries
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f1:
                f1.write(b"x" * 2048)
                path1 = f1.name
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f2:
                f2.write(b"x" * 2048)
                path2 = f2.name

            try:
                mock_tts._cache = {
                    "abc123def456": path1,
                    "xyz789uvw012": path2,
                }
                MockTTS.return_value = mock_tts

                _run_cache_status(verbose=True)

                captured = capsys.readouterr()
                assert "Cached entries:" in captured.out
                assert "abc123def456"[:12] in captured.out
                assert "xyz789uvw012"[:12] in captured.out
            finally:
                os.unlink(path1)
                os.unlink(path2)

    def test_verbose_missing_file(self, capsys):
        """Verbose mode handles missing cache files gracefully."""
        with mock.patch("io_mcp.__main__.IoMcpConfig") as MockConfig, \
             mock.patch("io_mcp.__main__.TTSEngine") as MockTTS:
            MockConfig.load.return_value = FakeConfig()
            mock_tts = mock.MagicMock()
            mock_tts.cache_stats.return_value = (1, 0)
            mock_tts._cache = {
                "abc123def456": "/nonexistent/path.wav",
            }
            MockTTS.return_value = mock_tts

            _run_cache_status(verbose=True)

            captured = capsys.readouterr()
            assert "(missing)" in captured.out

    def test_empty_cache(self, capsys):
        """Empty cache shows zero items."""
        with mock.patch("io_mcp.__main__.IoMcpConfig") as MockConfig, \
             mock.patch("io_mcp.__main__.TTSEngine") as MockTTS:
            MockConfig.load.return_value = FakeConfig()
            mock_tts = mock.MagicMock()
            mock_tts.cache_stats.return_value = (0, 0)
            mock_tts._cache = {}
            MockTTS.return_value = mock_tts

            _run_cache_status(verbose=False)

            captured = capsys.readouterr()
            assert "0" in captured.out
            assert "0 B" in captured.out


# ─── Tests: _run_cache_warmup ────────────────────────────────────────


class TestRunCacheWarmup:
    """Test _run_cache_warmup with mocked TTSEngine."""

    def _make_mock_tts(self, cache_keys=None):
        """Create a mock TTSEngine for warmup tests."""
        mock_tts = mock.MagicMock()
        mock_tts._cache = cache_keys or {}
        mock_tts._cache_key = mock.MagicMock(
            side_effect=lambda text, voice_override=None: f"{text}:{voice_override}"
        )
        mock_tts.cache_stats.return_value = (0, 0)
        mock_tts._generate_to_file_unlocked = mock.MagicMock(return_value="/tmp/fake.wav")
        return mock_tts

    def test_dry_run_skips_generation(self, capsys):
        """Dry run prints summary but does not generate audio."""
        with mock.patch("io_mcp.__main__.IoMcpConfig") as MockConfig, \
             mock.patch("io_mcp.__main__.TTSEngine") as MockTTS:
            MockConfig.load.return_value = FakeConfig()
            mock_tts = self._make_mock_tts()
            MockTTS.return_value = mock_tts

            _run_cache_warmup(dry_run=True)

            captured = capsys.readouterr()
            assert "dry run" in captured.out.lower()
            assert "skipping generation" in captured.out.lower()
            # Should NOT have called generate
            mock_tts._generate_to_file_unlocked.assert_not_called()

    def test_dry_run_verbose_lists_items(self, capsys):
        """Dry run with verbose lists each item to generate."""
        with mock.patch("io_mcp.__main__.IoMcpConfig") as MockConfig, \
             mock.patch("io_mcp.__main__.TTSEngine") as MockTTS:
            MockConfig.load.return_value = FakeConfig()
            mock_tts = self._make_mock_tts()
            MockTTS.return_value = mock_tts

            _run_cache_warmup(verbose=True, dry_run=True)

            captured = capsys.readouterr()
            assert "Items to generate:" in captured.out
            # Should contain at least some known items
            assert "Record response" in captured.out
            assert "(default)" in captured.out

    def test_warmup_header_shows_voice(self, capsys):
        """Warmup output includes voice info."""
        with mock.patch("io_mcp.__main__.IoMcpConfig") as MockConfig, \
             mock.patch("io_mcp.__main__.TTSEngine") as MockTTS:
            MockConfig.load.return_value = FakeConfig()
            mock_tts = self._make_mock_tts()
            MockTTS.return_value = mock_tts

            _run_cache_warmup(dry_run=True)

            captured = capsys.readouterr()
            assert "agent voice: sage" in captured.out

    def test_separate_ui_voice_doubles_items(self, capsys):
        """With a separate UI voice, total items doubles."""
        with mock.patch("io_mcp.__main__.IoMcpConfig") as MockConfig, \
             mock.patch("io_mcp.__main__.TTSEngine") as MockTTS:
            config = FakeConfigWithUIVoice()
            MockConfig.load.return_value = config
            mock_tts = self._make_mock_tts()
            MockTTS.return_value = mock_tts

            _run_cache_warmup(dry_run=True)

            captured = capsys.readouterr()
            assert "UI voice: teo" in captured.out
            # Total items should be 2x the string count
            # Look for the "Total items" line
            for line in captured.out.splitlines():
                if "Total items" in line:
                    total_str = line.split(":")[-1].strip()
                    total = int(total_str)
                    break
            for line in captured.out.splitlines():
                if "Total strings" in line:
                    strings_str = line.split(":")[-1].strip()
                    strings = int(strings_str)
                    break
            assert total == strings * 2

    def test_all_cached_prints_nothing_to_do(self, capsys):
        """When everything is cached, prints 'Nothing to do'."""
        with mock.patch("io_mcp.__main__.IoMcpConfig") as MockConfig, \
             mock.patch("io_mcp.__main__.TTSEngine") as MockTTS, \
             mock.patch("io_mcp.__main__._collect_warmup_texts") as MockTexts:
            config = FakeConfig()
            MockConfig.load.return_value = config
            MockTexts.return_value = ["hello", "world"]

            mock_tts = mock.MagicMock()
            # Everything is "cached" — all keys exist in the cache dict
            mock_tts._cache = {
                "hello:None": "/tmp/hello.wav",
                "world:None": "/tmp/world.wav",
            }
            mock_tts._cache_key = mock.MagicMock(
                side_effect=lambda text, voice_override=None: f"{text}:{voice_override}"
            )
            mock_tts.cache_stats.return_value = (2, 4096)
            MockTTS.return_value = mock_tts

            _run_cache_warmup()

            captured = capsys.readouterr()
            assert "Nothing to do" in captured.out
            assert "Already cached: 2" in captured.out
            mock_tts._generate_to_file_unlocked.assert_not_called()

    def test_warmup_generates_uncached_items(self, capsys):
        """Warmup calls _generate_to_file_unlocked for uncached items."""
        with mock.patch("io_mcp.__main__.IoMcpConfig") as MockConfig, \
             mock.patch("io_mcp.__main__.TTSEngine") as MockTTS, \
             mock.patch("io_mcp.__main__._collect_warmup_texts") as MockTexts:
            config = FakeConfig()
            MockConfig.load.return_value = config
            MockTexts.return_value = ["hello", "world"]

            mock_tts = mock.MagicMock()
            mock_tts._cache = {}
            mock_tts._cache_key = mock.MagicMock(
                side_effect=lambda text, voice_override=None: f"{text}:{voice_override}"
            )
            mock_tts._generate_to_file_unlocked = mock.MagicMock(return_value="/tmp/fake.wav")
            mock_tts.cache_stats.return_value = (2, 4096)
            MockTTS.return_value = mock_tts

            _run_cache_warmup()

            captured = capsys.readouterr()
            assert "Generated 2 items" in captured.out
            assert "0 errors" in captured.out
            assert mock_tts._generate_to_file_unlocked.call_count == 2

    def test_warmup_handles_generation_errors(self, capsys):
        """Warmup counts errors when generation fails."""
        with mock.patch("io_mcp.__main__.IoMcpConfig") as MockConfig, \
             mock.patch("io_mcp.__main__.TTSEngine") as MockTTS, \
             mock.patch("io_mcp.__main__._collect_warmup_texts") as MockTexts:
            config = FakeConfig()
            MockConfig.load.return_value = config
            MockTexts.return_value = ["hello", "world"]

            mock_tts = mock.MagicMock()
            mock_tts._cache = {}
            mock_tts._cache_key = mock.MagicMock(
                side_effect=lambda text, voice_override=None: f"{text}:{voice_override}"
            )
            # First succeeds, second fails
            mock_tts._generate_to_file_unlocked = mock.MagicMock(
                side_effect=["/tmp/fake.wav", None]
            )
            mock_tts.cache_stats.return_value = (1, 2048)
            MockTTS.return_value = mock_tts

            _run_cache_warmup()

            captured = capsys.readouterr()
            assert "1 errors" in captured.out

    def test_warmup_handles_exception_in_generation(self, capsys):
        """Warmup handles exceptions from _generate_to_file_unlocked gracefully."""
        with mock.patch("io_mcp.__main__.IoMcpConfig") as MockConfig, \
             mock.patch("io_mcp.__main__.TTSEngine") as MockTTS, \
             mock.patch("io_mcp.__main__._collect_warmup_texts") as MockTexts:
            config = FakeConfig()
            MockConfig.load.return_value = config
            MockTexts.return_value = ["hello"]

            mock_tts = mock.MagicMock()
            mock_tts._cache = {}
            mock_tts._cache_key = mock.MagicMock(
                side_effect=lambda text, voice_override=None: f"{text}:{voice_override}"
            )
            mock_tts._generate_to_file_unlocked = mock.MagicMock(
                side_effect=RuntimeError("API down")
            )
            mock_tts.cache_stats.return_value = (0, 0)
            MockTTS.return_value = mock_tts

            # Should not raise
            _run_cache_warmup()

            captured = capsys.readouterr()
            assert "1 errors" in captured.out


# ─── Tests: _run_cache_command argument parsing ──────────────────────


class TestRunCacheCommand:
    """Test CLI argument parsing for 'io-mcp cache'."""

    def test_warmup_dispatches(self, capsys):
        """'cache warmup' dispatches to _run_cache_warmup."""
        import sys
        with mock.patch.object(sys, "argv", ["io-mcp", "cache", "warmup"]), \
             mock.patch("io_mcp.__main__._run_cache_warmup") as mock_warmup:
            _run_cache_command()
            mock_warmup.assert_called_once_with(verbose=False, dry_run=False)

    def test_status_dispatches(self, capsys):
        """'cache status' dispatches to _run_cache_status."""
        import sys
        with mock.patch.object(sys, "argv", ["io-mcp", "cache", "status"]), \
             mock.patch("io_mcp.__main__._run_cache_status") as mock_status:
            _run_cache_command()
            mock_status.assert_called_once_with(verbose=False)

    def test_warmup_verbose_flag(self, capsys):
        """'cache warmup -v' passes verbose=True."""
        import sys
        with mock.patch.object(sys, "argv", ["io-mcp", "cache", "warmup", "-v"]), \
             mock.patch("io_mcp.__main__._run_cache_warmup") as mock_warmup:
            _run_cache_command()
            mock_warmup.assert_called_once_with(verbose=True, dry_run=False)

    def test_warmup_dry_run_flag(self, capsys):
        """'cache warmup --dry-run' passes dry_run=True."""
        import sys
        with mock.patch.object(sys, "argv", ["io-mcp", "cache", "warmup", "--dry-run"]), \
             mock.patch("io_mcp.__main__._run_cache_warmup") as mock_warmup:
            _run_cache_command()
            mock_warmup.assert_called_once_with(verbose=False, dry_run=True)

    def test_warmup_verbose_and_dry_run(self, capsys):
        """'cache warmup -v --dry-run' passes both flags."""
        import sys
        with mock.patch.object(sys, "argv", ["io-mcp", "cache", "warmup", "-v", "--dry-run"]), \
             mock.patch("io_mcp.__main__._run_cache_warmup") as mock_warmup:
            _run_cache_command()
            mock_warmup.assert_called_once_with(verbose=True, dry_run=True)

    def test_missing_action_exits(self):
        """Missing action arg causes SystemExit."""
        import sys
        with mock.patch.object(sys, "argv", ["io-mcp", "cache"]):
            with pytest.raises(SystemExit):
                _run_cache_command()

    def test_invalid_action_exits(self):
        """Invalid action arg causes SystemExit."""
        import sys
        with mock.patch.object(sys, "argv", ["io-mcp", "cache", "invalid"]):
            with pytest.raises(SystemExit):
                _run_cache_command()

    def test_status_verbose_flag(self, capsys):
        """'cache status -v' passes verbose=True."""
        import sys
        with mock.patch.object(sys, "argv", ["io-mcp", "cache", "status", "--verbose"]), \
             mock.patch("io_mcp.__main__._run_cache_status") as mock_status:
            _run_cache_command()
            mock_status.assert_called_once_with(verbose=True)


# ─── Tests: Enhanced _run_cache_status ────────────────────────────────


class TestRunCacheStatusEnhanced:
    """Test enhanced _run_cache_status features: directory info, disk stats,
    config section, file age, verbose disk listing."""

    def _mock_context(self, config=None, tts_cache=None, cache_stats=(0, 0)):
        """Set up mock patches for IoMcpConfig and TTSEngine."""
        cfg = config or FakeConfig()
        mock_tts = mock.MagicMock()
        mock_tts.cache_stats.return_value = cache_stats
        mock_tts._cache = tts_cache or {}
        return cfg, mock_tts

    def test_shows_directory_path(self, capsys):
        """Output includes the cache directory path."""
        cfg, mock_tts = self._mock_context()
        with mock.patch("io_mcp.__main__.IoMcpConfig") as MockConfig, \
             mock.patch("io_mcp.__main__.TTSEngine") as MockTTS, \
             mock.patch("io_mcp.__main__.os.path.isdir", return_value=False), \
             mock.patch("io_mcp.__main__.os.scandir", return_value=iter([])):
            MockConfig.load.return_value = cfg
            MockTTS.return_value = mock_tts

            _run_cache_status(verbose=False)

            captured = capsys.readouterr()
            assert "Directory:" in captured.out

    def test_shows_exists_yes(self, capsys):
        """Shows 'Exists: yes' when cache directory exists."""
        cfg, mock_tts = self._mock_context()
        with mock.patch("io_mcp.__main__.IoMcpConfig") as MockConfig, \
             mock.patch("io_mcp.__main__.TTSEngine") as MockTTS, \
             mock.patch("io_mcp.__main__.os.path.isdir", return_value=True), \
             mock.patch("io_mcp.__main__.os.scandir", return_value=iter([])):
            MockConfig.load.return_value = cfg
            MockTTS.return_value = mock_tts

            _run_cache_status(verbose=False)

            captured = capsys.readouterr()
            assert "yes" in captured.out

    def test_shows_exists_no(self, capsys):
        """Shows 'Exists: no' when cache directory does not exist."""
        cfg, mock_tts = self._mock_context()
        with mock.patch("io_mcp.__main__.IoMcpConfig") as MockConfig, \
             mock.patch("io_mcp.__main__.TTSEngine") as MockTTS, \
             mock.patch("io_mcp.__main__.os.path.isdir", return_value=False):
            MockConfig.load.return_value = cfg
            MockTTS.return_value = mock_tts

            _run_cache_status(verbose=False)

            captured = capsys.readouterr()
            assert "Exists:" in captured.out
            assert "no" in captured.out

    def test_shows_config_section(self, capsys):
        """Output includes Config section with voice, model, speed."""
        cfg, mock_tts = self._mock_context()
        with mock.patch("io_mcp.__main__.IoMcpConfig") as MockConfig, \
             mock.patch("io_mcp.__main__.TTSEngine") as MockTTS, \
             mock.patch("io_mcp.__main__.os.path.isdir", return_value=False):
            MockConfig.load.return_value = cfg
            MockTTS.return_value = mock_tts

            _run_cache_status(verbose=False)

            captured = capsys.readouterr()
            assert "Config:" in captured.out
            assert "Voice:" in captured.out
            assert "sage" in captured.out
            assert "Model:" in captured.out
            assert "gpt-4o-mini-tts" in captured.out
            assert "Speed:" in captured.out
            assert "1.3" in captured.out

    def test_shows_emotion_in_config(self, capsys):
        """Config section includes emotion when set."""
        cfg, mock_tts = self._mock_context()
        with mock.patch("io_mcp.__main__.IoMcpConfig") as MockConfig, \
             mock.patch("io_mcp.__main__.TTSEngine") as MockTTS, \
             mock.patch("io_mcp.__main__.os.path.isdir", return_value=False):
            MockConfig.load.return_value = cfg
            MockTTS.return_value = mock_tts

            _run_cache_status(verbose=False)

            captured = capsys.readouterr()
            assert "Emotion:" in captured.out
            assert "friendly" in captured.out

    def test_shows_ui_voice_when_different(self, capsys):
        """Config section shows UI voice when it differs from agent voice."""
        cfg = FakeConfigWithUIVoice()
        _, mock_tts = self._mock_context()
        with mock.patch("io_mcp.__main__.IoMcpConfig") as MockConfig, \
             mock.patch("io_mcp.__main__.TTSEngine") as MockTTS, \
             mock.patch("io_mcp.__main__.os.path.isdir", return_value=False):
            MockConfig.load.return_value = cfg
            MockTTS.return_value = mock_tts

            _run_cache_status(verbose=False)

            captured = capsys.readouterr()
            assert "UI voice:" in captured.out
            assert "teo" in captured.out

    def test_hides_ui_voice_when_same(self, capsys):
        """Config section omits UI voice when it matches agent voice."""
        cfg, mock_tts = self._mock_context()
        # FakeConfig has no separate UI voice
        with mock.patch("io_mcp.__main__.IoMcpConfig") as MockConfig, \
             mock.patch("io_mcp.__main__.TTSEngine") as MockTTS, \
             mock.patch("io_mcp.__main__.os.path.isdir", return_value=False):
            MockConfig.load.return_value = cfg
            MockTTS.return_value = mock_tts

            _run_cache_status(verbose=False)

            captured = capsys.readouterr()
            assert "UI voice:" not in captured.out

    def test_disk_stats_with_files(self, capsys, tmp_path):
        """Disk section shows file count, total size, avg, oldest, newest."""
        import time

        # Create fake WAV files with different times
        f1 = tmp_path / "aaa.wav"
        f2 = tmp_path / "bbb.wav"
        f1.write_bytes(b"x" * 1024)
        f2.write_bytes(b"x" * 3072)

        # Set different mtimes
        old_time = time.time() - 3600  # 1 hour ago
        new_time = time.time()
        os.utime(f1, (old_time, old_time))
        os.utime(f2, (new_time, new_time))

        cfg, mock_tts = self._mock_context()
        with mock.patch("io_mcp.__main__.IoMcpConfig") as MockConfig, \
             mock.patch("io_mcp.__main__.TTSEngine") as MockTTS, \
             mock.patch("io_mcp.__main__.os.path.isdir", return_value=True), \
             mock.patch("io_mcp.__main__.os.scandir") as mock_scandir:
            MockConfig.load.return_value = cfg
            MockTTS.return_value = mock_tts

            # Create mock DirEntry objects
            entries = []
            for fpath in [f1, f2]:
                entry = mock.MagicMock()
                entry.is_file.return_value = True
                entry.name = fpath.name
                entry.path = str(fpath)
                st = os.stat(fpath)
                entry.stat.return_value = st
                entries.append(entry)
            mock_scandir.return_value = iter(entries)

            _run_cache_status(verbose=False)

            captured = capsys.readouterr()
            assert "Disk:" in captured.out
            assert "Files:" in captured.out
            assert "2" in captured.out  # 2 files
            assert "Avg:" in captured.out
            assert "Oldest:" in captured.out
            assert "Newest:" in captured.out

    def test_disk_stats_total_size(self, capsys, tmp_path):
        """Disk total size is the sum of all WAV file sizes."""
        f1 = tmp_path / "a.wav"
        f1.write_bytes(b"x" * 2048)

        cfg, mock_tts = self._mock_context()
        with mock.patch("io_mcp.__main__.IoMcpConfig") as MockConfig, \
             mock.patch("io_mcp.__main__.TTSEngine") as MockTTS, \
             mock.patch("io_mcp.__main__.os.path.isdir", return_value=True), \
             mock.patch("io_mcp.__main__.os.scandir") as mock_scandir:
            MockConfig.load.return_value = cfg
            MockTTS.return_value = mock_tts

            entry = mock.MagicMock()
            entry.is_file.return_value = True
            entry.name = "a.wav"
            entry.path = str(f1)
            entry.stat.return_value = os.stat(f1)
            mock_scandir.return_value = iter([entry])

            _run_cache_status(verbose=False)

            captured = capsys.readouterr()
            assert "2.0 KB" in captured.out

    def test_disk_section_hidden_when_dir_missing(self, capsys):
        """Disk section is not shown when cache directory doesn't exist."""
        cfg, mock_tts = self._mock_context()
        with mock.patch("io_mcp.__main__.IoMcpConfig") as MockConfig, \
             mock.patch("io_mcp.__main__.TTSEngine") as MockTTS, \
             mock.patch("io_mcp.__main__.os.path.isdir", return_value=False):
            MockConfig.load.return_value = cfg
            MockTTS.return_value = mock_tts

            _run_cache_status(verbose=False)

            captured = capsys.readouterr()
            assert "Disk:" not in captured.out

    def test_disk_empty_directory(self, capsys):
        """Disk section with empty directory shows 0 files."""
        cfg, mock_tts = self._mock_context()
        with mock.patch("io_mcp.__main__.IoMcpConfig") as MockConfig, \
             mock.patch("io_mcp.__main__.TTSEngine") as MockTTS, \
             mock.patch("io_mcp.__main__.os.path.isdir", return_value=True), \
             mock.patch("io_mcp.__main__.os.scandir", return_value=iter([])):
            MockConfig.load.return_value = cfg
            MockTTS.return_value = mock_tts

            _run_cache_status(verbose=False)

            captured = capsys.readouterr()
            assert "Disk:" in captured.out
            assert "Files:   0" in captured.out
            # Avg, Oldest, Newest should NOT appear for empty dir
            assert "Avg:" not in captured.out
            assert "Oldest:" not in captured.out
            assert "Newest:" not in captured.out

    def test_non_wav_files_ignored(self, capsys, tmp_path):
        """Only .wav files are counted in disk stats."""
        wav = tmp_path / "test.wav"
        txt = tmp_path / "test.txt"
        wav.write_bytes(b"x" * 1024)
        txt.write_bytes(b"y" * 512)

        cfg, mock_tts = self._mock_context()
        with mock.patch("io_mcp.__main__.IoMcpConfig") as MockConfig, \
             mock.patch("io_mcp.__main__.TTSEngine") as MockTTS, \
             mock.patch("io_mcp.__main__.os.path.isdir", return_value=True), \
             mock.patch("io_mcp.__main__.os.scandir") as mock_scandir:
            MockConfig.load.return_value = cfg
            MockTTS.return_value = mock_tts

            entries = []
            for fpath in [wav, txt]:
                entry = mock.MagicMock()
                entry.is_file.return_value = True
                entry.name = fpath.name
                entry.path = str(fpath)
                entry.stat.return_value = os.stat(fpath)
                entries.append(entry)
            mock_scandir.return_value = iter(entries)

            _run_cache_status(verbose=False)

            captured = capsys.readouterr()
            # Only 1 WAV counted
            assert "Files:   1" in captured.out

    def test_verbose_shows_disk_files(self, capsys, tmp_path):
        """Verbose mode includes 'Disk files:' listing."""
        f1 = tmp_path / "abc123.wav"
        f1.write_bytes(b"x" * 512)

        cfg, mock_tts = self._mock_context()
        with mock.patch("io_mcp.__main__.IoMcpConfig") as MockConfig, \
             mock.patch("io_mcp.__main__.TTSEngine") as MockTTS, \
             mock.patch("io_mcp.__main__.os.path.isdir", return_value=True), \
             mock.patch("io_mcp.__main__.os.scandir") as mock_scandir:
            MockConfig.load.return_value = cfg
            MockTTS.return_value = mock_tts

            entry = mock.MagicMock()
            entry.is_file.return_value = True
            entry.name = "abc123.wav"
            entry.path = str(f1)
            st = os.stat(f1)
            entry.stat.return_value = st
            mock_scandir.return_value = iter([entry])

            _run_cache_status(verbose=True)

            captured = capsys.readouterr()
            assert "Disk files:" in captured.out
            assert "abc123.wav" in captured.out

    def test_verbose_no_disk_files_section_when_empty(self, capsys):
        """Verbose mode skips 'Disk files:' when no files on disk."""
        cfg, mock_tts = self._mock_context()
        with mock.patch("io_mcp.__main__.IoMcpConfig") as MockConfig, \
             mock.patch("io_mcp.__main__.TTSEngine") as MockTTS, \
             mock.patch("io_mcp.__main__.os.path.isdir", return_value=True), \
             mock.patch("io_mcp.__main__.os.scandir", return_value=iter([])):
            MockConfig.load.return_value = cfg
            MockTTS.return_value = mock_tts

            _run_cache_status(verbose=True)

            captured = capsys.readouterr()
            assert "Disk files:" not in captured.out

    def test_scandir_oserror_handled(self, capsys):
        """OSError from scandir is handled gracefully."""
        cfg, mock_tts = self._mock_context()
        with mock.patch("io_mcp.__main__.IoMcpConfig") as MockConfig, \
             mock.patch("io_mcp.__main__.TTSEngine") as MockTTS, \
             mock.patch("io_mcp.__main__.os.path.isdir", return_value=True), \
             mock.patch("io_mcp.__main__.os.scandir", side_effect=OSError("perm")):
            MockConfig.load.return_value = cfg
            MockTTS.return_value = mock_tts

            # Should not raise
            _run_cache_status(verbose=False)

            captured = capsys.readouterr()
            assert "Files:   0" in captured.out

    def test_stat_oserror_on_entry_handled(self, capsys, tmp_path):
        """OSError from entry.stat() is handled — file still counted."""
        cfg, mock_tts = self._mock_context()
        with mock.patch("io_mcp.__main__.IoMcpConfig") as MockConfig, \
             mock.patch("io_mcp.__main__.TTSEngine") as MockTTS, \
             mock.patch("io_mcp.__main__.os.path.isdir", return_value=True), \
             mock.patch("io_mcp.__main__.os.scandir") as mock_scandir:
            MockConfig.load.return_value = cfg
            MockTTS.return_value = mock_tts

            entry = mock.MagicMock()
            entry.is_file.return_value = True
            entry.name = "bad.wav"
            entry.path = "/tmp/bad.wav"
            entry.stat.side_effect = OSError("gone")
            mock_scandir.return_value = iter([entry])

            _run_cache_status(verbose=False)

            captured = capsys.readouterr()
            # File is still counted (with size 0, mtime 0)
            assert "Files:   1" in captured.out

    def test_no_emotion_hides_emotion_line(self, capsys):
        """Config section omits emotion when it's empty."""
        cfg, mock_tts = self._mock_context()
        cfg.tts_emotion = ""
        with mock.patch("io_mcp.__main__.IoMcpConfig") as MockConfig, \
             mock.patch("io_mcp.__main__.TTSEngine") as MockTTS, \
             mock.patch("io_mcp.__main__.os.path.isdir", return_value=False):
            MockConfig.load.return_value = cfg
            MockTTS.return_value = mock_tts

            _run_cache_status(verbose=False)

            captured = capsys.readouterr()
            assert "Emotion:" not in captured.out

    def test_disk_files_sorted_newest_first_in_verbose(self, capsys, tmp_path):
        """Verbose disk listing is sorted by newest first."""
        import time

        f_old = tmp_path / "old_file.wav"
        f_new = tmp_path / "new_file.wav"
        f_old.write_bytes(b"x" * 100)
        f_new.write_bytes(b"y" * 200)

        old_time = time.time() - 7200
        new_time = time.time()
        os.utime(f_old, (old_time, old_time))
        os.utime(f_new, (new_time, new_time))

        cfg, mock_tts = self._mock_context()
        with mock.patch("io_mcp.__main__.IoMcpConfig") as MockConfig, \
             mock.patch("io_mcp.__main__.TTSEngine") as MockTTS, \
             mock.patch("io_mcp.__main__.os.path.isdir", return_value=True), \
             mock.patch("io_mcp.__main__.os.scandir") as mock_scandir:
            MockConfig.load.return_value = cfg
            MockTTS.return_value = mock_tts

            entries = []
            for fpath in [f_old, f_new]:
                entry = mock.MagicMock()
                entry.is_file.return_value = True
                entry.name = fpath.name
                entry.path = str(fpath)
                st = os.stat(fpath)
                entry.stat.return_value = st
                entries.append(entry)
            mock_scandir.return_value = iter(entries)

            _run_cache_status(verbose=True)

            captured = capsys.readouterr()
            lines = captured.out.splitlines()
            # Find the disk files listing
            disk_lines = []
            in_disk_files = False
            for line in lines:
                if "Disk files:" in line:
                    in_disk_files = True
                    continue
                if in_disk_files:
                    stripped = line.strip()
                    if stripped.startswith("new_file") or stripped.startswith("old_file"):
                        disk_lines.append(stripped)
                    elif stripped and not line.startswith("    "):
                        break
            # new_file should appear before old_file (newest first)
            assert len(disk_lines) == 2
            assert "new_file" in disk_lines[0]
            assert "old_file" in disk_lines[1]
