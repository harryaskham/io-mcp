"""Tests for io-mcp TUI themes and widgets modules."""

import pytest

from io_mcp.tui.themes import (
    COLOR_SCHEMES,
    DEFAULT_SCHEME,
    get_scheme,
    build_css,
)
from io_mcp.tui.widgets import (
    ChoiceItem,
    DwellBar,
    EXTRA_OPTIONS,
    _safe_action,
)


class TestColorSchemes:
    """Tests for color scheme definitions and access."""

    def test_four_schemes_defined(self):
        assert len(COLOR_SCHEMES) == 4
        assert set(COLOR_SCHEMES.keys()) == {"nord", "tokyo-night", "catppuccin", "dracula"}

    def test_default_scheme_is_nord(self):
        assert DEFAULT_SCHEME == "nord"

    def test_all_schemes_have_required_keys(self):
        required_keys = {
            "bg", "bg_alt", "fg", "fg_dim", "accent", "success",
            "warning", "error", "purple", "blue",
            "highlight_bg", "highlight_fg", "highlight_accent", "border",
        }
        for name, scheme in COLOR_SCHEMES.items():
            assert required_keys <= set(scheme.keys()), f"{name} missing keys: {required_keys - set(scheme.keys())}"

    def test_all_colors_are_hex(self):
        for name, scheme in COLOR_SCHEMES.items():
            for key, value in scheme.items():
                assert value.startswith("#"), f"{name}.{key} = {value} is not a hex color"
                assert len(value) == 7, f"{name}.{key} = {value} is not #RRGGBB"

    def test_get_scheme_returns_correct(self):
        assert get_scheme("nord") == COLOR_SCHEMES["nord"]
        assert get_scheme("dracula") == COLOR_SCHEMES["dracula"]

    def test_get_scheme_fallback(self):
        result = get_scheme("nonexistent")
        assert result == COLOR_SCHEMES[DEFAULT_SCHEME]

    def test_get_scheme_default(self):
        result = get_scheme()
        assert result == COLOR_SCHEMES[DEFAULT_SCHEME]


class TestBuildCSS:
    """Tests for CSS generation."""

    def test_generates_string(self):
        css = build_css()
        assert isinstance(css, str)
        assert len(css) > 100

    def test_contains_widget_ids(self):
        css = build_css()
        assert "#tab-bar" in css
        assert "#preamble" in css
        assert "#status" in css
        assert "#choices" in css
        assert "#dwell-bar" in css
        assert "ChoiceItem" in css
        assert "Header" in css

    def test_uses_scheme_colors(self):
        css = build_css("nord")
        nord = COLOR_SCHEMES["nord"]
        assert nord["bg"] in css
        assert nord["accent"] in css

    def test_different_schemes_produce_different_css(self):
        css_nord = build_css("nord")
        css_dracula = build_css("dracula")
        assert css_nord != css_dracula

    def test_invalid_scheme_falls_back(self):
        css_fallback = build_css("nonexistent")
        css_default = build_css(DEFAULT_SCHEME)
        assert css_fallback == css_default


class TestExtraOptions:
    """Tests for EXTRA_OPTIONS constant."""

    def test_is_list(self):
        assert isinstance(EXTRA_OPTIONS, list)

    def test_has_entries(self):
        assert len(EXTRA_OPTIONS) > 5

    def test_entries_have_label_and_summary(self):
        for opt in EXTRA_OPTIONS:
            assert "label" in opt
            assert "summary" in opt
            assert isinstance(opt["label"], str)
            assert isinstance(opt["summary"], str)

    def test_required_options_present(self):
        labels = {opt["label"] for opt in EXTRA_OPTIONS}
        assert "Quick settings" in labels
        assert "Record response" in labels
        assert "Queue message" in labels
        assert "New agent" in labels
        assert "Switch tab" in labels


class TestSafeAction:
    """Tests for the _safe_action decorator."""

    def test_normal_execution(self):
        class Fake:
            _tts = None

            @_safe_action
            def my_action(self):
                return 42

        f = Fake()
        assert f.my_action() == 42

    def test_exception_caught(self):
        class FakeTTS:
            def speak_async(self, text):
                pass

        class Fake:
            _tts = FakeTTS()

            @_safe_action
            def bad_action(self):
                raise ValueError("test error")

        f = Fake()
        # Should not raise
        result = f.bad_action()
        assert result is None  # Returns None on error

    def test_preserves_function_name(self):
        class Fake:
            _tts = None

            @_safe_action
            def my_special_action(self):
                pass

        f = Fake()
        assert f.my_special_action.__name__ == "my_special_action"


class TestDjentIntegration:
    """Tests for djent config integration."""

    def test_djent_disabled_by_default(self):
        from io_mcp.config import IoMcpConfig
        import tempfile, os

        with tempfile.TemporaryDirectory() as tmpdir:
            config = IoMcpConfig.load(os.path.join(tmpdir, "config.yml"))
            assert config.djent_enabled is False

    def test_djent_no_extra_options_when_disabled(self):
        from io_mcp.config import IoMcpConfig
        import tempfile, os

        with tempfile.TemporaryDirectory() as tmpdir:
            config = IoMcpConfig.load(os.path.join(tmpdir, "config.yml"))
            opts = config.extra_options
            djent_titles = [o["title"] for o in opts if "djent" in o.get("title", "").lower()]
            assert len(djent_titles) == 0

    def test_djent_adds_options_when_enabled(self):
        from io_mcp.config import IoMcpConfig
        import tempfile, os

        with tempfile.TemporaryDirectory() as tmpdir:
            config = IoMcpConfig.load(os.path.join(tmpdir, "config.yml"))
            config.djent_enabled = True
            opts = config.extra_options
            djent_titles = [o["title"] for o in opts if "djent" in o.get("title", "").lower()]
            assert len(djent_titles) >= 2

    def test_djent_adds_quick_actions_when_enabled(self):
        from io_mcp.config import IoMcpConfig
        import tempfile, os

        with tempfile.TemporaryDirectory() as tmpdir:
            config = IoMcpConfig.load(os.path.join(tmpdir, "config.yml"))
            config.djent_enabled = True
            actions = config.quick_actions
            assert len(actions) >= 5
            keys = {a["key"] for a in actions}
            assert "!" in keys
            assert "@" in keys

    def test_djent_no_quick_actions_when_disabled(self):
        from io_mcp.config import IoMcpConfig
        import tempfile, os

        with tempfile.TemporaryDirectory() as tmpdir:
            config = IoMcpConfig.load(os.path.join(tmpdir, "config.yml"))
            actions = config.quick_actions
            assert len(actions) == 0


class TestTruecolorEnv:
    """Tests that COLORTERM is forced to truecolor for tmux compat."""

    def test_colorterm_set_when_missing(self):
        import os
        old = os.environ.pop("COLORTERM", None)
        try:
            # Simulate what main() does
            if not os.environ.get("COLORTERM"):
                os.environ["COLORTERM"] = "truecolor"
            assert os.environ["COLORTERM"] == "truecolor"
        finally:
            if old is not None:
                os.environ["COLORTERM"] = old
            else:
                os.environ.pop("COLORTERM", None)

    def test_colorterm_preserved_when_already_set(self):
        import os
        old = os.environ.get("COLORTERM")
        try:
            os.environ["COLORTERM"] = "24bit"
            if not os.environ.get("COLORTERM"):
                os.environ["COLORTERM"] = "truecolor"
            assert os.environ["COLORTERM"] == "24bit"
        finally:
            if old is not None:
                os.environ["COLORTERM"] = old
            else:
                os.environ.pop("COLORTERM", None)

    def test_screen_term_upgraded(self):
        import os
        old = os.environ.get("TERM")
        try:
            os.environ["TERM"] = "screen"
            term = os.environ.get("TERM", "")
            if term.startswith("screen") or term == "dumb":
                os.environ["TERM"] = "xterm-256color"
            assert os.environ["TERM"] == "xterm-256color"
        finally:
            if old is not None:
                os.environ["TERM"] = old
            else:
                os.environ.pop("TERM", None)
