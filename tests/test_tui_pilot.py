"""Textual pilot tests for io-mcp TUI.

Tests the core UI interactions using Textual's async test framework.
Focuses on widget state rather than full present_choices flow (which
requires threading that doesn't work well in test contexts).
"""

import pytest
import threading

from textual.widgets import ListView

from io_mcp.tui.app import IoMcpApp
from io_mcp.tui.widgets import ChoiceItem, SubmitTextArea, EXTRA_OPTIONS
from io_mcp.session import Session


class MockTTS:
    """Minimal TTS mock that does nothing."""

    def __init__(self):
        self._muted = False
        self._speed = 1.0
        self._process = None
        self._lock = threading.Lock()
        self._cache = {}

    def speak(self, text, **kwargs): pass
    def speak_async(self, text, **kwargs): pass
    def speak_streaming(self, text, **kwargs): pass
    def speak_with_espeak_fallback(self, text, **kwargs): pass
    def stop(self): pass
    def play_chime(self, name): pass
    def pregenerate(self, texts): pass
    def clear_cache(self): pass
    def is_cached(self, text, **kwargs): return False
    def mute(self): self._muted = True
    def unmute(self): self._muted = False


def make_app(**kwargs) -> IoMcpApp:
    """Create a testable IoMcpApp with mocked TTS."""
    tts = MockTTS()
    return IoMcpApp(tts=tts, freeform_tts=tts, demo=True, **kwargs)


@pytest.mark.asyncio
async def test_app_starts():
    """App mounts without crashing."""
    app = make_app()
    async with app.run_test():
        status = app.query_one("#status")
        assert status.display is True


@pytest.mark.asyncio
async def test_show_choices_populates_listview():
    """_show_choices fills the ListView with extras + real choices."""
    app = make_app()
    async with app.run_test() as pilot:
        session, _ = app.manager.get_or_create("test-1")
        session.registered = True
        session.name = "Test"
        app.on_session_created(session)

        # Set up session state as if present_choices was called
        session.preamble = "Pick one"
        session.choices = [
            {"label": "Alpha", "summary": "First"},
            {"label": "Beta", "summary": "Second"},
        ]
        session.active = True
        session.extras_count = len(EXTRA_OPTIONS)
        session.all_items = list(EXTRA_OPTIONS) + session.choices

        # Call _show_choices directly (normally called via call_from_thread)
        app._show_choices()
        await pilot.pause(0.1)

        list_view = app.query_one("#choices", ListView)
        assert list_view.display is True

        items = [c for c in list_view.children if isinstance(c, ChoiceItem)]
        real_items = [c for c in items if c.choice_index > 0]
        extra_items = [c for c in items if c.choice_index <= 0]

        assert len(real_items) == 2
        assert len(extra_items) == len(EXTRA_OPTIONS)


@pytest.mark.asyncio
async def test_extra_option_index_mapping():
    """Extra option logical indices map correctly to EXTRA_OPTIONS array."""
    app = make_app()
    async with app.run_test() as pilot:
        session, _ = app.manager.get_or_create("test-1")
        session.registered = True
        session.name = "Test"
        app.on_session_created(session)

        session.preamble = "Test"
        session.choices = [{"label": "Choice1", "summary": ""}]
        session.active = True
        session.extras_count = len(EXTRA_OPTIONS)
        session.all_items = list(EXTRA_OPTIONS) + session.choices

        app._show_choices()
        await pilot.pause(0.1)

        list_view = app.query_one("#choices", ListView)
        items = [c for c in list_view.children if isinstance(c, ChoiceItem)]
        extra_items = [c for c in items if c.choice_index <= 0]

        # Verify every extra item maps to a valid EXTRA_OPTIONS entry
        for item in extra_items:
            ei = len(EXTRA_OPTIONS) - 1 + item.choice_index
            assert 0 <= ei < len(EXTRA_OPTIONS), (
                f"choice_index={item.choice_index} maps to ei={ei}, "
                f"out of range [0, {len(EXTRA_OPTIONS)})"
            )


@pytest.mark.asyncio
async def test_default_focus_is_first_real_choice():
    """Focus defaults to the first real choice, not an extra option."""
    app = make_app()
    async with app.run_test() as pilot:
        session, _ = app.manager.get_or_create("test-1")
        session.registered = True
        session.name = "Test"
        app.on_session_created(session)

        session.preamble = "Test"
        session.choices = [
            {"label": "Alpha", "summary": ""},
            {"label": "Beta", "summary": ""},
        ]
        session.active = True
        session.extras_count = len(EXTRA_OPTIONS)
        session.all_items = list(EXTRA_OPTIONS) + session.choices

        app._show_choices()
        await pilot.pause(0.1)

        list_view = app.query_one("#choices", ListView)
        # Focus should be on the first real choice
        assert list_view.index == len(EXTRA_OPTIONS)


@pytest.mark.asyncio
async def test_freeform_input_opens_and_closes():
    """Pressing 'i' opens freeform input, Escape closes it."""
    app = make_app()
    async with app.run_test() as pilot:
        session, _ = app.manager.get_or_create("test-1")
        session.registered = True
        session.name = "Test"
        app.on_session_created(session)

        session.preamble = "Test"
        session.choices = [{"label": "Choice1", "summary": ""}]
        session.active = True
        session.extras_count = len(EXTRA_OPTIONS)
        session.all_items = list(EXTRA_OPTIONS) + session.choices

        app._show_choices()
        await pilot.pause(0.1)

        # Open freeform
        await pilot.press("i")
        await pilot.pause(0.2)

        inp = app.query_one("#freeform-input", SubmitTextArea)
        assert inp.styles.display != "none"
        assert session.input_mode is True

        # Close with Escape
        await pilot.press("escape")
        await pilot.pause(0.2)

        assert session.input_mode is False


@pytest.mark.asyncio
async def test_tab_switch_noop_single_session():
    """Tab switching is a no-op with only one session."""
    app = make_app()
    async with app.run_test() as pilot:
        session, _ = app.manager.get_or_create("test-1")
        session.registered = True
        session.name = "Agent1"
        app.on_session_created(session)

        active_before = app.manager.active_session_id

        await pilot.press("l")
        await pilot.pause(0.1)
        assert app.manager.active_session_id == active_before

        await pilot.press("h")
        await pilot.pause(0.1)
        assert app.manager.active_session_id == active_before


@pytest.mark.asyncio
async def test_settings_menu_opens():
    """Pressing 's' opens the settings menu."""
    app = make_app()
    async with app.run_test() as pilot:
        session, _ = app.manager.get_or_create("test-1")
        session.registered = True
        session.name = "Test"
        app.on_session_created(session)

        await pilot.press("s")
        await pilot.pause(0.2)

        assert app._in_settings is True

        list_view = app.query_one("#choices", ListView)
        items = [c for c in list_view.children if isinstance(c, ChoiceItem)]
        labels = [c.choice_label for c in items]
        assert any("Speed" in l for l in labels)
        assert any("Agent voice" in l for l in labels)


@pytest.mark.asyncio
async def test_message_mode_opens():
    """Pressing 'm' opens message input mode."""
    app = make_app()
    async with app.run_test() as pilot:
        session, _ = app.manager.get_or_create("test-1")
        session.registered = True
        session.name = "Test"
        app.on_session_created(session)

        await pilot.press("m")
        await pilot.pause(0.2)

        assert app._message_mode is True

        inp = app.query_one("#freeform-input", SubmitTextArea)
        assert inp.styles.display != "none"

        # Escape cancels
        await pilot.press("escape")
        await pilot.pause(0.2)
        assert app._message_mode is False
