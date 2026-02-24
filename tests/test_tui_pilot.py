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
    def speak_with_local_fallback(self, text, **kwargs): pass
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
        # Collapsed mode: "More options ›" + "Record response" = 2 visible extras
        assert len(extra_items) == 2


@pytest.mark.asyncio
async def test_extra_option_index_mapping():
    """Extra option labels match the collapsed extras (More options + Record response)."""
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

        # Collapsed mode: should have "More options ›" and "Record response"
        labels = [item.choice_label for item in extra_items]
        assert "More options ›" in labels
        assert "Record response" in labels


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
        # Focus should be on the first real choice (after collapsed extras: More + Record = 2)
        assert list_view.index == 2


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


# ─── Inbox two-column layout tests ──────────────────────────────────────


@pytest.mark.asyncio
async def test_inbox_list_hidden_with_single_item():
    """Inbox list (left pane) is hidden when there's only 1 inbox item."""
    app = make_app()
    async with app.run_test() as pilot:
        session, _ = app.manager.get_or_create("test-1")
        session.registered = True
        session.name = "Test"
        app.on_session_created(session)

        # Set up single active choice
        session.preamble = "Pick one"
        session.choices = [
            {"label": "Alpha", "summary": "First"},
            {"label": "Beta", "summary": "Second"},
        ]
        session.active = True
        session.extras_count = len(EXTRA_OPTIONS)
        session.all_items = list(EXTRA_OPTIONS) + session.choices

        app._show_choices()
        await pilot.pause(0.1)

        inbox_list = app.query_one("#inbox-list", ListView)
        assert inbox_list.display is False

        # Main content should be visible
        main_content = app.query_one("#main-content")
        assert main_content.display is True


@pytest.mark.asyncio
async def test_inbox_list_visible_with_multiple_items():
    """Inbox list (left pane) is visible when there are multiple inbox items."""
    from io_mcp.session import InboxItem

    app = make_app()
    async with app.run_test() as pilot:
        session, _ = app.manager.get_or_create("test-1")
        session.registered = True
        session.name = "Test"
        app.on_session_created(session)

        # Enqueue two inbox items
        item1 = InboxItem(kind="choices", preamble="First question", choices=[{"label": "A", "summary": ""}])
        item2 = InboxItem(kind="choices", preamble="Second question", choices=[{"label": "B", "summary": ""}])
        session.enqueue(item1)
        session.enqueue(item2)

        # Set up session as if first item is active
        session.preamble = item1.preamble
        session.choices = list(item1.choices)
        session.active = True
        session._active_inbox_item = item1
        session.extras_count = len(EXTRA_OPTIONS)
        session.all_items = list(EXTRA_OPTIONS) + session.choices

        app._show_choices()
        await pilot.pause(0.1)

        inbox_list = app.query_one("#inbox-list", ListView)
        assert inbox_list.display is True

        # Should have 2 items in inbox list
        from io_mcp.tui.widgets import InboxListItem
        inbox_items = [c for c in inbox_list.children if isinstance(c, InboxListItem)]
        assert len(inbox_items) == 2


@pytest.mark.asyncio
async def test_inbox_pane_focus_default_is_choices():
    """Default focus should be on the right (choices) pane."""
    app = make_app()
    async with app.run_test() as pilot:
        session, _ = app.manager.get_or_create("test-1")
        session.registered = True
        session.name = "Test"
        app.on_session_created(session)

        session.preamble = "Pick one"
        session.choices = [{"label": "Alpha", "summary": ""}]
        session.active = True
        session.extras_count = len(EXTRA_OPTIONS)
        session.all_items = list(EXTRA_OPTIONS) + session.choices

        app._show_choices()
        await pilot.pause(0.1)

        assert app._inbox_pane_focused is False


@pytest.mark.asyncio
async def test_main_content_hidden_initially():
    """Main content container is hidden when no agent is connected."""
    app = make_app()
    async with app.run_test() as pilot:
        main_content = app.query_one("#main-content")
        assert main_content.display is False
