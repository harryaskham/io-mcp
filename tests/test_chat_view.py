"""Tests for chat view layout behavior in io-mcp TUI.

Verifies that the chat view guards and layout logic work correctly:
- _populate_chat_choices_list populates #chat-choices correctly
- _show_waiting hides #chat-choices in chat view mode
- _show_choices delegates to _populate_chat_choices_list in chat view
- _show_session_waiting returns early in chat view mode
- Auto-scroll respects user scroll position
- _notify_chat_feed_update triggers immediate refresh
"""

import pytest
import time

from textual.widgets import ListView

from io_mcp.tui.app import IoMcpApp
from io_mcp.tui.widgets import ChoiceItem, EXTRA_OPTIONS, PRIMARY_EXTRAS
from io_mcp.session import Session, SpeechEntry


# ─── Reuse the test helpers from test_tui_pilot ─────────────────────

from tests.test_tui_pilot import MockTTS, make_app


def _setup_session_with_choices(app, session_id="test-1", name="Test",
                                 choices=None):
    """Helper: create a registered session with active choices."""
    session, _ = app.manager.get_or_create(session_id)
    session.registered = True
    session.name = name
    app.on_session_created(session)

    if choices is None:
        choices = [
            {"label": "Alpha", "summary": "First option"},
            {"label": "Beta", "summary": "Second option"},
            {"label": "Gamma", "summary": "Third option"},
        ]
    session.preamble = "Pick one"
    session.choices = choices
    session.active = True
    session.extras_count = len(EXTRA_OPTIONS)
    session.all_items = list(EXTRA_OPTIONS) + choices
    return session


# ─── Test: _populate_chat_choices_list ─────────────────────────────


@pytest.mark.asyncio
async def test_populate_chat_choices_list_correct_item_count():
    """_populate_chat_choices_list fills #chat-choices with choices + PRIMARY_EXTRAS."""
    app = make_app()
    async with app.run_test() as pilot:
        session = _setup_session_with_choices(app)

        # Call the method directly
        app._populate_chat_choices_list(session)
        await pilot.pause(0.1)

        chat_lv = app.query_one("#chat-choices", ListView)
        assert chat_lv.display is True

        items = [c for c in chat_lv.children if isinstance(c, ChoiceItem)]

        # Should have 3 real choices + len(PRIMARY_EXTRAS) extras
        real = [c for c in items if c.choice_index > 0]
        extras = [c for c in items if c.choice_index <= 0]

        assert len(real) == 3
        assert len(extras) == len(PRIMARY_EXTRAS)
        assert len(items) == 3 + len(PRIMARY_EXTRAS)

        # Real choices should have correct labels
        assert real[0].choice_label == "Alpha"
        assert real[1].choice_label == "Beta"
        assert real[2].choice_label == "Gamma"

        # ListView should have a valid focused index (0 or adjusted by Textual)
        assert chat_lv.index is not None
        assert 0 <= chat_lv.index < len(items)


@pytest.mark.asyncio
async def test_populate_chat_choices_list_empty_choices():
    """_populate_chat_choices_list with no choices still shows PRIMARY_EXTRAS."""
    app = make_app()
    async with app.run_test() as pilot:
        session = _setup_session_with_choices(app, choices=[])

        app._populate_chat_choices_list(session)
        await pilot.pause(0.1)

        chat_lv = app.query_one("#chat-choices", ListView)
        assert chat_lv.display is True

        items = [c for c in chat_lv.children if isinstance(c, ChoiceItem)]
        real = [c for c in items if c.choice_index > 0]
        extras = [c for c in items if c.choice_index <= 0]

        assert len(real) == 0
        assert len(extras) == len(PRIMARY_EXTRAS)


# ─── Test: _show_waiting hides #chat-choices in chat view ──────────


@pytest.mark.asyncio
async def test_show_waiting_hides_chat_choices_in_chat_view():
    """_show_waiting in chat view mode hides #chat-choices."""
    app = make_app()
    async with app.run_test() as pilot:
        session = _setup_session_with_choices(app)

        # First, populate chat choices so they're visible
        app._chat_view_active = True
        app._populate_chat_choices_list(session)
        await pilot.pause(0.1)

        chat_lv = app.query_one("#chat-choices", ListView)
        assert chat_lv.display is True

        # Now call _show_waiting — it should hide #chat-choices
        app._show_waiting("Selected: Alpha")
        await pilot.pause(0.1)

        assert chat_lv.display is False


# ─── Test: _show_choices calls _populate_chat_choices_list ──────────


@pytest.mark.asyncio
async def test_show_choices_calls_populate_in_chat_view(monkeypatch):
    """_show_choices in chat view mode calls _populate_chat_choices_list."""
    app = make_app()
    async with app.run_test() as pilot:
        session = _setup_session_with_choices(app)

        # Activate chat view mode
        app._chat_view_active = True

        # Track whether _populate_chat_choices_list was called
        populate_called_with = []
        original_populate = app._populate_chat_choices_list

        def mock_populate(sess):
            populate_called_with.append(sess)
            original_populate(sess)

        monkeypatch.setattr(app, "_populate_chat_choices_list", mock_populate)

        # Call _show_choices
        app._show_choices()
        await pilot.pause(0.1)

        # It should have been called once with the active session
        assert len(populate_called_with) == 1
        assert populate_called_with[0] is session


@pytest.mark.asyncio
async def test_show_choices_hides_chat_choices_when_no_active_choices(monkeypatch):
    """_show_choices in chat view hides #chat-choices when session has no active choices."""
    app = make_app()
    async with app.run_test() as pilot:
        session = _setup_session_with_choices(app)

        # Activate chat view and populate choices first
        app._chat_view_active = True
        app._populate_chat_choices_list(session)
        await pilot.pause(0.1)
        assert app.query_one("#chat-choices", ListView).display is True

        # Now clear choices and mark session inactive
        session.active = False
        session.choices = []

        app._show_choices()
        await pilot.pause(0.1)

        assert app.query_one("#chat-choices", ListView).display is False


# ─── Test: _show_session_waiting returns early in chat view ──────────


@pytest.mark.asyncio
async def test_show_session_waiting_returns_early_in_chat_view():
    """_show_session_waiting is a no-op when chat view is active."""
    app = make_app()
    async with app.run_test() as pilot:
        session = _setup_session_with_choices(app)

        # Show choices first (in normal mode) to establish visible state
        app._show_choices()
        await pilot.pause(0.1)

        # Enable chat view
        app._chat_view_active = True

        # Hide status to verify _show_session_waiting doesn't re-show it
        status = app.query_one("#status")
        status.display = False

        # Call _show_session_waiting — should return early
        app._show_session_waiting(session)
        await pilot.pause(0.1)

        # Status should still be hidden (the method returned early)
        assert status.display is False


@pytest.mark.asyncio
async def test_show_session_waiting_works_when_chat_view_off():
    """_show_session_waiting runs normally when chat view is NOT active."""
    app = make_app()
    async with app.run_test() as pilot:
        session = _setup_session_with_choices(app)

        # Ensure chat view is off
        app._chat_view_active = False

        # Call _show_session_waiting
        app._show_session_waiting(session)
        await pilot.pause(0.1)

        # Status should be visible and contain the session name
        status = app.query_one("#status")
        assert status.display is True


# ─── Test: _chat_auto_scroll defaults to True ───────────────────────


@pytest.mark.asyncio
async def test_chat_auto_scroll_defaults_to_true():
    """_chat_auto_scroll should default to True."""
    app = make_app()
    async with app.run_test() as pilot:
        assert app._chat_auto_scroll is True


# ─── Test: _chat_feed_is_at_bottom ──────────────────────────────────


@pytest.mark.asyncio
async def test_chat_feed_is_at_bottom_empty_feed():
    """_chat_feed_is_at_bottom returns True when feed is empty (no scroll)."""
    app = make_app()
    async with app.run_test() as pilot:
        assert app._chat_feed_is_at_bottom() is True


@pytest.mark.asyncio
async def test_chat_feed_is_at_bottom_with_few_items():
    """_chat_feed_is_at_bottom returns True when content fits without scroll."""
    app = make_app()
    async with app.run_test() as pilot:
        session = _setup_session_with_choices(app)

        # Add a few speech entries
        session.speech_log.append(SpeechEntry(text="Hello"))
        session.speech_log.append(SpeechEntry(text="World"))

        app._chat_view_active = True
        app._build_chat_feed(session)
        await pilot.pause(0.1)

        # With only a few items, there's no scroll — should be "at bottom"
        assert app._chat_feed_is_at_bottom() is True


# ─── Test: _build_chat_feed scrolls to bottom by default ────────────


@pytest.mark.asyncio
async def test_build_chat_feed_scrolls_to_bottom_default():
    """_build_chat_feed scrolls to bottom when at bottom (default state)."""
    app = make_app()
    async with app.run_test() as pilot:
        session = _setup_session_with_choices(app)

        # Add some speech entries
        for i in range(5):
            session.speech_log.append(SpeechEntry(text=f"Message {i}"))

        app._chat_view_active = True
        app._chat_auto_scroll = True
        app._build_chat_feed(session)
        await pilot.pause(0.1)

        feed = app.query_one("#chat-feed", ListView)
        # Feed should have items (header + 5 speech entries)
        assert len(feed.children) > 0
        # auto_scroll should remain True
        assert app._chat_auto_scroll is True


# ─── Test: _notify_chat_feed_update ──────────────────────────────────


@pytest.mark.asyncio
async def test_notify_chat_feed_update_noop_when_chat_inactive():
    """_notify_chat_feed_update does nothing when chat view is not active."""
    app = make_app()
    async with app.run_test() as pilot:
        session = _setup_session_with_choices(app)

        app._chat_view_active = False
        refresh_calls = []
        original_refresh = app._refresh_chat_feed

        def mock_refresh():
            refresh_calls.append(True)
            original_refresh()

        app._refresh_chat_feed = mock_refresh

        app._notify_chat_feed_update(session)
        await pilot.pause(0.1)

        # Should NOT have called _refresh_chat_feed
        assert len(refresh_calls) == 0


@pytest.mark.asyncio
async def test_notify_chat_feed_update_triggers_refresh_when_active():
    """_notify_chat_feed_update triggers immediate refresh when chat is active."""
    app = make_app()
    async with app.run_test() as pilot:
        session = _setup_session_with_choices(app)

        app._chat_view_active = True
        app._chat_content_hash = "some-old-hash"

        # Build an initial feed so fingerprint has something to compare
        app._build_chat_feed(session)
        await pilot.pause(0.1)

        feed = app.query_one("#chat-feed", ListView)
        initial_hash = app._chat_content_hash

        # Add a new speech entry to change the fingerprint
        session.speech_log.append(SpeechEntry(text="New speech"))

        # Call _notify_chat_feed_update — should force rebuild
        app._notify_chat_feed_update(session)
        await pilot.pause(0.1)

        # The hash should have been cleared and then rebuilt
        # (it's now different from "" because _refresh_chat_feed sets it)
        assert app._chat_content_hash != ""
        assert app._chat_content_hash != initial_hash


@pytest.mark.asyncio
async def test_notify_chat_feed_update_skips_unfocused_session():
    """_notify_chat_feed_update skips refresh for non-focused session in single mode."""
    app = make_app()
    async with app.run_test() as pilot:
        session1 = _setup_session_with_choices(app, session_id="s1", name="Session1")
        session2, _ = app.manager.get_or_create("s2")
        session2.registered = True
        session2.name = "Session2"

        app._chat_view_active = True
        app._chat_unified = False  # Single-session mode

        # Focus is on session1 (it was created first and is active)
        refresh_calls = []
        original_refresh = app._refresh_chat_feed

        def mock_refresh():
            refresh_calls.append(True)
            original_refresh()

        app._refresh_chat_feed = mock_refresh

        # Notify about session2 (not focused) — should skip
        app._notify_chat_feed_update(session2)
        await pilot.pause(0.1)

        assert len(refresh_calls) == 0


@pytest.mark.asyncio
async def test_notify_chat_feed_update_refreshes_in_unified_mode():
    """_notify_chat_feed_update refreshes for any session in unified mode."""
    app = make_app()
    async with app.run_test() as pilot:
        session1 = _setup_session_with_choices(app, session_id="s1", name="Session1")
        session2, _ = app.manager.get_or_create("s2")
        session2.registered = True
        session2.name = "Session2"

        app._chat_view_active = True
        app._chat_unified = True  # Unified mode

        # Build initial feed
        all_sessions = list(app.manager.all_sessions())
        app._build_chat_feed(session1, sessions=all_sessions)
        await pilot.pause(0.1)

        old_hash = app._chat_content_hash

        # Add speech to session2 and notify
        session2.speech_log.append(SpeechEntry(text="From session 2"))
        app._notify_chat_feed_update(session2)
        await pilot.pause(0.1)

        # Hash should have changed (refresh happened)
        assert app._chat_content_hash != old_hash


# ─── Test: action_chat_view resets auto_scroll ───────────────────────


@pytest.mark.asyncio
async def test_action_chat_view_resets_auto_scroll():
    """Opening chat view resets _chat_auto_scroll to True."""
    app = make_app()
    async with app.run_test() as pilot:
        session = _setup_session_with_choices(app)

        # Manually set auto_scroll to False (as if user scrolled up)
        app._chat_auto_scroll = False

        # Toggle chat view on
        app.action_chat_view()
        await pilot.pause(0.1)

        # auto_scroll should be reset to True
        assert app._chat_auto_scroll is True


# ─── Test: _show_idle in chat view rebuilds feed and hides choices ────


@pytest.mark.asyncio
async def test_show_idle_in_chat_view_rebuilds_feed_and_hides_choices():
    """_show_idle in chat view should rebuild chat feed and hide #chat-choices."""
    app = make_app()
    async with app.run_test() as pilot:
        session = _setup_session_with_choices(app)

        # Activate chat view and populate choices
        app._chat_view_active = True
        app._populate_chat_choices_list(session)
        await pilot.pause(0.1)

        chat_lv = app.query_one("#chat-choices", ListView)
        assert chat_lv.display is True

        # Track refresh calls
        refresh_called = []
        original_refresh = app._refresh_chat_feed

        def mock_refresh():
            refresh_called.append(True)
            original_refresh()

        app._refresh_chat_feed = mock_refresh

        # Call _show_idle (as if switching to a session without choices)
        app._show_idle()
        await pilot.pause(0.1)

        # #chat-choices should be hidden
        assert chat_lv.display is False
        # Chat feed should have been rebuilt
        assert len(refresh_called) == 1


# ─── Test: action_next_tab skips inbox logic in chat view ────────────


@pytest.mark.asyncio
async def test_action_next_tab_skips_inbox_in_chat_view():
    """action_next_tab in chat view should skip inbox expand logic."""
    app = make_app()
    async with app.run_test() as pilot:
        # Create two sessions so tab switching is possible
        session1 = _setup_session_with_choices(app, session_id="s1", name="S1")
        session2 = _setup_session_with_choices(app, session_id="s2", name="S2")

        # Activate chat view
        app._chat_view_active = True

        # Set inbox as collapsed — in normal mode, l would expand it
        app._inbox_collapsed = True

        # Focus session1
        app.manager.focus("s1")

        # Press next_tab — should switch tab, NOT expand inbox
        app.action_next_tab()
        await pilot.pause(0.1)

        # Should have switched to session2
        assert app.manager.active_session_id == "s2"
        # inbox_collapsed should still be True (we skipped inbox logic)
        assert app._inbox_collapsed is True


# ─── Test: action_next_choices_tab works in chat view ────────────────


@pytest.mark.asyncio
async def test_action_next_choices_tab_in_chat_view():
    """action_next_choices_tab in chat view switches session and updates chat-choices."""
    app = make_app()
    async with app.run_test() as pilot:
        # Create two sessions with choices
        session1 = _setup_session_with_choices(
            app, session_id="s1", name="S1",
            choices=[{"label": "A1", "summary": "a1"}],
        )
        session2 = _setup_session_with_choices(
            app, session_id="s2", name="S2",
            choices=[{"label": "B1", "summary": "b1"}, {"label": "B2", "summary": "b2"}],
        )

        # Activate chat view
        app._chat_view_active = True

        # Focus session1 and populate choices
        app.manager.focus("s1")
        app._populate_chat_choices_list(session1)
        await pilot.pause(0.1)

        chat_lv = app.query_one("#chat-choices", ListView)
        # Verify session1 choices
        real = [c for c in chat_lv.children if isinstance(c, ChoiceItem) and c.choice_index > 0]
        assert len(real) == 1
        assert real[0].choice_label == "A1"

        # Press n — should switch to session2
        app.action_next_choices_tab()
        await pilot.pause(0.1)

        # Should have switched to session2
        assert app.manager.active_session_id == "s2"

        # #chat-choices should now show session2's choices
        chat_lv = app.query_one("#chat-choices", ListView)
        assert chat_lv.display is True
        real = [c for c in chat_lv.children if isinstance(c, ChoiceItem) and c.choice_index > 0]
        assert len(real) == 2
        assert real[0].choice_label == "B1"
        assert real[1].choice_label == "B2"


# ─── Test: action_prev_tab skips inbox logic in chat view ────────────


@pytest.mark.asyncio
async def test_action_prev_tab_skips_inbox_in_chat_view():
    """action_prev_tab in chat view should skip inbox collapse/expand logic."""
    app = make_app()
    async with app.run_test() as pilot:
        # Create two sessions
        session1 = _setup_session_with_choices(app, session_id="s1", name="S1")
        session2 = _setup_session_with_choices(app, session_id="s2", name="S2")

        # Activate chat view
        app._chat_view_active = True

        # Focus session2
        app.manager.focus("s2")

        # Press prev_tab — should switch to session1
        app.action_prev_tab()
        await pilot.pause(0.1)

        # Should have switched to session1
        assert app.manager.active_session_id == "s1"
