"""Textual pilot tests for io-mcp TUI.

Tests the core UI interactions using Textual's async test framework.
Focuses on widget state rather than full present_choices flow (which
requires threading that doesn't work well in test contexts).
"""

import pytest
import threading

from textual.widgets import ListView

from io_mcp.tui.app import IoMcpApp
from io_mcp.tui.widgets import ChoiceItem, SubmitTextArea, TextInputModal, EXTRA_OPTIONS, PRIMARY_EXTRAS
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
        # Collapsed mode: "More options ›" + PRIMARY_EXTRAS visible
        assert len(extra_items) == len(PRIMARY_EXTRAS) + 1


@pytest.mark.asyncio
async def test_extra_option_index_mapping():
    """Extra option labels match the collapsed extras (More options + PRIMARY_EXTRAS)."""
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

        # Collapsed mode: should have "More options ›" + all PRIMARY_EXTRAS
        labels = [item.choice_label for item in extra_items]
        assert "More options ›" in labels
        for pe in PRIMARY_EXTRAS:
            assert pe["label"] in labels


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
        # Focus should be on the first real choice (after collapsed extras: More + PRIMARY_EXTRAS)
        assert list_view.index == len(PRIMARY_EXTRAS) + 1


@pytest.mark.asyncio
async def test_freeform_input_opens_and_closes():
    """Pressing 'i' opens freeform input modal, Escape closes it."""
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

        # Open freeform — should push a TextInputModal
        await pilot.press("i")
        await pilot.pause(0.2)

        assert session.input_mode is True
        assert isinstance(app.screen, TextInputModal)

        # Close with Escape — modal should be dismissed
        await pilot.press("escape")
        await pilot.pause(0.2)

        assert session.input_mode is False
        assert not isinstance(app.screen, TextInputModal)


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
    """Pressing 'm' opens message input modal."""
    app = make_app()
    async with app.run_test() as pilot:
        session, _ = app.manager.get_or_create("test-1")
        session.registered = True
        session.name = "Test"
        app.on_session_created(session)

        await pilot.press("m")
        await pilot.pause(0.2)

        assert app._message_mode is True
        assert isinstance(app.screen, TextInputModal)

        # Escape cancels
        await pilot.press("escape")
        await pilot.pause(0.2)
        assert app._message_mode is False
        assert not isinstance(app.screen, TextInputModal)


# ─── Inbox two-column layout tests ──────────────────────────────────────


@pytest.mark.asyncio
async def test_inbox_list_hidden_with_single_item():
    """Inbox list (left pane) is hidden when user explicitly collapses it with 'b' key."""
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
        # Inbox pane is visible by default (user can collapse with 'b')
        # With single item it used to auto-hide, but now it stays visible
        # until user explicitly collapses it

        # Explicitly collapse
        app._inbox_collapsed = True
        app._update_inbox_list()
        await pilot.pause(0.1)
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
        # Ensure inbox is not collapsed for this test
        app._inbox_collapsed = False

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
async def test_inbox_items_show_agent_name_in_multi_agent_mode():
    """Inbox items show agent name prefix when multiple agents are connected."""
    from io_mcp.session import InboxItem

    app = make_app()
    async with app.run_test() as pilot:
        # Ensure inbox is not collapsed for this test
        app._inbox_collapsed = False

        # Create two agent sessions (multi-agent mode)
        session1, _ = app.manager.get_or_create("test-1")
        session1.registered = True
        session1.name = "Code Review"
        app.on_session_created(session1)

        session2, _ = app.manager.get_or_create("test-2")
        session2.registered = True
        session2.name = "Build Agent"
        app.on_session_created(session2)

        # Set up session1 with inbox items
        item1 = InboxItem(kind="choices", preamble="Pick a file", choices=[{"label": "A", "summary": ""}])
        session1.enqueue(item1)
        session1.preamble = item1.preamble
        session1.choices = list(item1.choices)
        session1.active = True
        session1._active_inbox_item = item1
        session1.extras_count = len(EXTRA_OPTIONS)
        session1.all_items = list(EXTRA_OPTIONS) + session1.choices

        app._show_choices()
        await pilot.pause(0.1)

        inbox_list = app.query_one("#inbox-list", ListView)
        assert inbox_list.display is True

        from io_mcp.tui.widgets import InboxListItem
        inbox_items = [c for c in inbox_list.children if isinstance(c, InboxListItem)]
        assert len(inbox_items) >= 1

        # In multi-agent mode, items should carry the session name
        assert inbox_items[0].session_name == "Code Review"
        assert inbox_items[0].accent_color != ""


@pytest.mark.asyncio
async def test_inbox_items_no_agent_name_in_single_agent_mode():
    """Inbox items do NOT show agent name when only one agent is connected.

    Uses multiple pending items so the inbox pane is visible
    (single-item mode auto-hides the inbox).
    """
    from io_mcp.session import InboxItem

    app = make_app()
    async with app.run_test() as pilot:
        # Ensure inbox is not collapsed for this test
        app._inbox_collapsed = False

        # Create only one agent session
        session, _ = app.manager.get_or_create("test-1")
        session.registered = True
        session.name = "Solo Agent"
        app.on_session_created(session)

        # Need 2+ pending choice items so inbox stays visible in single-agent mode
        item1 = InboxItem(kind="choices", preamble="Pick a file", choices=[{"label": "A", "summary": ""}])
        item2 = InboxItem(kind="choices", preamble="Pick a color", choices=[{"label": "Red", "summary": ""}])
        session.enqueue(item1)
        session.enqueue(item2)
        session.preamble = item1.preamble
        session.choices = list(item1.choices)
        session.active = True
        session._active_inbox_item = item1
        session.extras_count = len(EXTRA_OPTIONS)
        session.all_items = list(EXTRA_OPTIONS) + session.choices

        app._show_choices()
        await pilot.pause(0.1)

        inbox_list = app.query_one("#inbox-list", ListView)
        from io_mcp.tui.widgets import InboxListItem
        inbox_items = [c for c in inbox_list.children if isinstance(c, InboxListItem)]
        assert len(inbox_items) >= 1

        # In single-agent mode, items should NOT carry a session name
        assert inbox_items[0].session_name == ""


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
async def test_inbox_focus_syncs_with_widget_focus():
    """_inbox_pane_focused syncs when widget focus changes (e.g. via Tab key)."""
    from io_mcp.session import InboxItem

    app = make_app()
    async with app.run_test() as pilot:
        # Ensure inbox is not collapsed for this test
        app._inbox_collapsed = False

        session, _ = app.manager.get_or_create("test-1")
        session.registered = True
        session.name = "Test"
        app.on_session_created(session)

        # Enqueue two inbox items so inbox pane is visible
        item1 = InboxItem(kind="choices", preamble="Q1", choices=[{"label": "A", "summary": ""}])
        item2 = InboxItem(kind="choices", preamble="Q2", choices=[{"label": "B", "summary": ""}])
        session.enqueue(item1)
        session.enqueue(item2)

        session.preamble = item1.preamble
        session.choices = list(item1.choices)
        session.active = True
        session._active_inbox_item = item1
        session.extras_count = len(EXTRA_OPTIONS)
        session.all_items = list(EXTRA_OPTIONS) + session.choices

        app._show_choices()
        await pilot.pause(0.1)

        # Default: choices pane is focused
        assert app._inbox_pane_focused is False

        # Manually focus inbox list (simulates Tab key focus change)
        inbox_list = app.query_one("#inbox-list", ListView)
        inbox_list.focus()
        await pilot.pause(0.1)

        # _active_list_view should now return inbox list and sync state
        active = app._active_list_view()
        assert active.id == "inbox-list"
        assert app._inbox_pane_focused is True

        # Now focus choices list (simulates Tab back)
        choices_list = app.query_one("#choices", ListView)
        choices_list.focus()
        await pilot.pause(0.1)

        active = app._active_list_view()
        assert active.id == "choices"
        assert app._inbox_pane_focused is False


@pytest.mark.asyncio
async def test_main_content_hidden_initially():
    """Main content container is hidden when no agent is connected."""
    app = make_app()
    async with app.run_test() as pilot:
        main_content = app.query_one("#main-content")
        assert main_content.display is False


# ─── Settings force-exit tests (bd-dj0) ───────────────────────────────


@pytest.mark.asyncio
async def test_clear_all_modal_state_resets_settings():
    """_clear_all_modal_state clears all settings/menu state flags."""
    app = make_app()
    async with app.run_test() as pilot:
        session, _ = app.manager.get_or_create("test-1")
        session.registered = True
        session.name = "Test"
        app.on_session_created(session)

        # Simulate being deep in nested settings state
        app._in_settings = True
        app._setting_edit_mode = True
        app._help_mode = True
        app._history_mode = True
        app._tab_picker_mode = True
        app._quick_settings_mode = True
        app._spawn_options = [{"label": "test"}]
        app._quick_action_options = [{"label": "test"}]
        app._worktree_options = [{"label": "test"}]
        app._dialog_callback = lambda x: None
        app._dialog_buttons = [{"label": "OK"}]
        session.in_settings = True
        session.reading_options = True

        # Clear everything
        app._clear_all_modal_state(session=session)

        assert app._in_settings is False
        assert app._setting_edit_mode is False
        assert app._help_mode is False
        assert app._history_mode is False
        assert app._tab_picker_mode is False
        assert app._quick_settings_mode is False
        assert app._spawn_options is None
        assert app._quick_action_options is None
        assert app._worktree_options is None
        assert app._dialog_callback is None
        assert app._dialog_buttons == []
        assert session.in_settings is False
        assert session.reading_options is False


@pytest.mark.asyncio
async def test_exit_settings_uses_clear_all_modal_state():
    """_exit_settings clears dialog and worktree state (previously missing)."""
    app = make_app()
    async with app.run_test() as pilot:
        session, _ = app.manager.get_or_create("test-1")
        session.registered = True
        session.name = "Test"
        app.on_session_created(session)

        # Enter settings
        await pilot.press("s")
        await pilot.pause(0.2)
        assert app._in_settings is True

        # Simulate dialog and worktree state being set
        app._dialog_callback = lambda x: None
        app._dialog_buttons = [{"label": "OK"}]
        app._worktree_options = [{"label": "test"}]

        # Exit settings
        app._exit_settings()

        # Guard should be active immediately after exit
        assert app._in_settings is False
        assert app._dialog_callback is None
        assert app._dialog_buttons == []
        assert app._worktree_options is None
        assert app._settings_just_closed is True  # guard active

        # Guard clears after timer (100ms)
        await pilot.pause(0.3)
        assert app._settings_just_closed is False


@pytest.mark.asyncio
async def test_choices_force_exit_settings():
    """Incoming choices force-exit settings and set the selection guard."""
    from io_mcp.session import InboxItem

    app = make_app()
    async with app.run_test() as pilot:
        session, _ = app.manager.get_or_create("test-1")
        session.registered = True
        session.name = "Test"
        app.on_session_created(session)

        # Enter settings
        await pilot.press("s")
        await pilot.pause(0.2)
        assert app._in_settings is True

        # Also simulate nested edit mode + dialog
        app._setting_edit_mode = True
        app._dialog_callback = lambda x: None
        app._dialog_buttons = [{"label": "OK"}]
        app._worktree_options = [{"label": "test"}]

        # Simulate choices arriving (prepare session state as _activate_and_present does)
        item = InboxItem(kind="choices", preamble="Pick one", choices=[
            {"label": "Alpha", "summary": "First"},
        ])
        session.preamble = "Pick one"
        session.choices = list(item.choices)
        session.selection = None
        session.selection_event.clear()
        session.active = True
        session.intro_speaking = True
        session.reading_options = False
        session.in_settings = False
        session._active_inbox_item = item

        # The force-exit code from _activate_and_present:
        is_fg = app._is_focused(session.session_id)
        if is_fg and app._in_settings:
            app._clear_all_modal_state(session=session)
            app._settings_just_closed = True
            app.set_timer(0.1, app._clear_settings_guard)

        # Verify all state is cleared
        assert app._in_settings is False
        assert app._setting_edit_mode is False
        assert app._dialog_callback is None
        assert app._dialog_buttons == []
        assert app._worktree_options is None
        assert app._settings_just_closed is True

        # Guard prevents _do_select from firing
        app._do_select()  # Should be a no-op due to guard
        assert session.selection is None  # No selection made

        # Guard clears after timer
        await pilot.pause(0.3)
        assert app._settings_just_closed is False


@pytest.mark.asyncio
async def test_choices_force_exit_setting_edit_mode():
    """Choices arriving during setting edit mode clear all edit state."""
    app = make_app()
    async with app.run_test() as pilot:
        session, _ = app.manager.get_or_create("test-1")
        session.registered = True
        session.name = "Test"
        app.on_session_created(session)

        # Enter settings, then enter edit mode
        await pilot.press("s")
        await pilot.pause(0.2)
        assert app._in_settings is True

        # Enter speed edit mode
        app._enter_setting_edit("speed")
        await pilot.pause(0.1)
        assert app._setting_edit_mode is True
        assert app._setting_edit_key == "speed"

        # Force-exit via _clear_all_modal_state
        app._clear_all_modal_state(session=session)

        assert app._in_settings is False
        assert app._setting_edit_mode is False
        assert session.in_settings is False


@pytest.mark.asyncio
async def test_choices_force_exit_quick_settings():
    """Choices arriving during quick settings mode clear all state."""
    app = make_app()
    async with app.run_test() as pilot:
        session, _ = app.manager.get_or_create("test-1")
        session.registered = True
        session.name = "Test"
        app.on_session_created(session)

        # Simulate quick settings mode
        app._in_settings = True
        app._quick_settings_mode = True
        session.in_settings = True

        # Force-exit
        app._clear_all_modal_state(session=session)

        assert app._in_settings is False
        assert app._quick_settings_mode is False
        assert session.in_settings is False

