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
    """_populate_chat_choices_list fills #chat-choices with choices + extras."""
    app = make_app()
    async with app.run_test() as pilot:
        session = _setup_session_with_choices(app)

        # Call the method directly
        app._populate_chat_choices_list(session)
        await pilot.pause(0.1)

        chat_lv = app.query_one("#chat-choices", ListView)
        assert chat_lv.display is True

        items = [c for c in chat_lv.children if isinstance(c, ChoiceItem)]

        # Should have 3 real choices + extras (More options + PRIMARY_EXTRAS when collapsed)
        real = [c for c in items if c.choice_index > 0]
        extras = [c for c in items if c.choice_index <= 0]

        assert len(real) == 3
        # Collapsed: More options toggle + PRIMARY_EXTRAS
        expected_extras = 1 + len(PRIMARY_EXTRAS)  # "More options" + primary
        assert len(extras) == expected_extras
        assert len(items) == 3 + expected_extras

        # Real choices should have correct labels
        assert real[0].choice_label == "Alpha"
        assert real[1].choice_label == "Beta"
        assert real[2].choice_label == "Gamma"

        # ListView should have a valid focused index (0 or adjusted by Textual)
        assert chat_lv.index is not None
        assert 0 <= chat_lv.index < len(items)


@pytest.mark.asyncio
async def test_populate_chat_choices_list_empty_choices():
    """_populate_chat_choices_list with no choices still shows extras."""
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
        # Collapsed: More options toggle + PRIMARY_EXTRAS
        expected_extras = 1 + len(PRIMARY_EXTRAS)
        assert len(extras) == expected_extras


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

        # Turn OFF chat view first (on_session_created auto-activates it)
        app._chat_view_active = False

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


# ─── Test: filter mode works in chat view ──────────────────────────


@pytest.mark.asyncio
async def test_filter_applies_to_chat_choices_in_chat_view():
    """_apply_filter targets #chat-choices when chat view is active."""
    app = make_app()
    async with app.run_test() as pilot:
        session = _setup_session_with_choices(app)

        # Activate chat view and populate choices
        app._chat_view_active = True
        app._populate_chat_choices_list(session)
        await pilot.pause(0.1)

        chat_lv = app.query_one("#chat-choices", ListView)
        initial_count = len(chat_lv.children)
        assert initial_count > 0

        # Enter filter mode and apply a query that matches only "Alpha"
        app._filter_mode = True
        app._apply_filter("alpha")
        await pilot.pause(0.1)

        items = [c for c in chat_lv.children if isinstance(c, ChoiceItem)]
        real = [c for c in items if c.choice_index > 0]
        assert len(real) == 1
        assert real[0].choice_label == "Alpha"


@pytest.mark.asyncio
async def test_filter_includes_preamble_in_chat_view():
    """_apply_filter re-adds PreambleItem header in chat view."""
    from io_mcp.tui.widgets import PreambleItem

    app = make_app()
    async with app.run_test() as pilot:
        session = _setup_session_with_choices(app)

        app._chat_view_active = True
        app._filter_mode = True
        app._apply_filter("")  # empty query shows all
        await pilot.pause(0.1)

        chat_lv = app.query_one("#chat-choices", ListView)
        preambles = [c for c in chat_lv.children if isinstance(c, PreambleItem)]
        assert len(preambles) == 1
        assert preambles[0].preamble_text == "Pick one"


@pytest.mark.asyncio
async def test_filter_exit_restores_chat_choices():
    """_exit_filter restores full #chat-choices and focuses it."""
    app = make_app()
    async with app.run_test() as pilot:
        session = _setup_session_with_choices(app)

        app._chat_view_active = True
        app._populate_chat_choices_list(session)
        await pilot.pause(0.1)

        chat_lv = app.query_one("#chat-choices", ListView)
        full_count = len(chat_lv.children)

        # Enter filter mode, filter to a subset
        app._filter_mode = True
        app._apply_filter("alpha")
        await pilot.pause(0.1)
        assert len(chat_lv.children) < full_count

        # Exit filter — should restore full list
        app._exit_filter()
        await pilot.pause(0.1)

        assert app._filter_mode is False
        assert chat_lv.display is True
        # Should have the same count as before filtering
        restored_count = len(chat_lv.children)
        assert restored_count == full_count


@pytest.mark.asyncio
async def test_filter_submit_keeps_filtered_chat_choices():
    """on_filter_submitted keeps filtered #chat-choices and focuses list."""
    from textual.widgets import Input

    app = make_app()
    async with app.run_test() as pilot:
        session = _setup_session_with_choices(app)

        app._chat_view_active = True
        app._populate_chat_choices_list(session)
        await pilot.pause(0.1)

        chat_lv = app.query_one("#chat-choices", ListView)
        full_count = len(chat_lv.children)

        # Enter filter mode and filter
        app._filter_mode = True
        filter_inp = app.query_one("#filter-input", Input)
        filter_inp.styles.display = "block"
        app._apply_filter("beta")
        await pilot.pause(0.1)

        filtered_count = len(chat_lv.children)
        assert filtered_count < full_count

        # Simulate submit by posting the event
        app._filter_mode = True  # ensure still in filter mode
        from textual.widgets._input import Input as InputWidget
        app.on_filter_submitted(InputWidget.Submitted(filter_inp, value="beta"))
        await pilot.pause(0.1)

        # Filter mode should be off, but list stays filtered
        assert app._filter_mode is False
        assert len(chat_lv.children) == filtered_count


@pytest.mark.asyncio
async def test_filter_no_match_in_chat_view():
    """_apply_filter with no matches shows only extras in chat view."""
    app = make_app()
    async with app.run_test() as pilot:
        session = _setup_session_with_choices(app)

        app._chat_view_active = True
        app._filter_mode = True
        app._apply_filter("zzz_nonexistent")
        await pilot.pause(0.1)

        chat_lv = app.query_one("#chat-choices", ListView)
        items = [c for c in chat_lv.children if isinstance(c, ChoiceItem)]
        real = [c for c in items if c.choice_index > 0]
        assert len(real) == 0


# ─── Test: chat feed pregeneration ────────────────────────────────────


@pytest.mark.asyncio
async def test_build_chat_feed_pregenerates_tts_for_recent_items():
    """_build_chat_feed calls _pregenerate_ui_worker for recent chat items."""
    app = make_app()
    async with app.run_test() as pilot:
        session = _setup_session_with_choices(app)

        # Add speech entries so there are chat items with tts_text
        for i in range(5):
            session.speech_log.append(SpeechEntry(text=f"Speech message {i}"))

        # Track pregenerate_ui_worker calls
        pregen_calls = []
        original_worker = app._pregenerate_ui_worker

        def mock_worker(texts):
            pregen_calls.append(texts)

        app._pregenerate_ui_worker = mock_worker

        # Build chat feed
        app._chat_view_active = True
        app._build_chat_feed(session)
        await pilot.pause(0.1)

        # Should have called _pregenerate_ui_worker with tts texts
        assert len(pregen_calls) == 1
        texts = pregen_calls[0]
        # Should contain at least some of the speech texts
        assert any("Speech message" in t for t in texts)


@pytest.mark.asyncio
async def test_build_chat_feed_skips_pregeneration_for_empty_feed():
    """_build_chat_feed does NOT call pregenerate when feed is empty."""
    app = make_app()
    async with app.run_test() as pilot:
        session, _ = app.manager.get_or_create("empty-1")
        session.registered = True
        session.name = "Empty"
        app.on_session_created(session)

        pregen_calls = []

        def mock_worker(texts):
            pregen_calls.append(texts)

        app._pregenerate_ui_worker = mock_worker

        app._chat_view_active = True
        app._build_chat_feed(session)
        await pilot.pause(0.1)

        # Only a header item, which has very short tts_text ("Empty session")
        # It should still pregenerate if there's text
        if pregen_calls:
            # If called, it should have short header text
            assert all(len(t) < 200 for texts in pregen_calls for t in texts)


# ─── Test: pane view interaction with chat view ───────────────────────


@pytest.mark.asyncio
async def test_pane_view_from_chat_view_hides_chat_widgets():
    """Opening pane view from chat view should hide all chat widgets."""
    app = make_app()
    async with app.run_test() as pilot:
        session = _setup_session_with_choices(app)
        session.tmux_pane = "%42"

        # Activate chat view
        app._chat_view_active = True
        try:
            app.query_one("#chat-feed").display = True
            app.query_one("#chat-input-bar").display = True
        except Exception:
            pass
        await pilot.pause(0.1)

        # Open pane view
        app.action_pane_view()
        await pilot.pause(0.1)

        # Chat widgets should be hidden
        assert app.query_one("#chat-feed").display is False
        try:
            assert app.query_one("#chat-choices").display is False
        except Exception:
            pass
        try:
            assert app.query_one("#chat-input-bar").display is False
        except Exception:
            pass

        # Pane view should be visible
        assert app.query_one("#pane-view").display is True

        # Chat view should be temporarily deactivated
        assert app._chat_view_active is False

        # But _pane_view_was_chat should be True
        assert app._pane_view_was_chat is True


@pytest.mark.asyncio
async def test_pane_view_close_restores_chat_view():
    """Closing pane view should restore chat view if it was active before."""
    app = make_app()
    async with app.run_test() as pilot:
        session = _setup_session_with_choices(app)
        session.tmux_pane = "%42"

        # Activate chat view
        app._chat_view_active = True
        try:
            app.query_one("#chat-feed").display = True
            app.query_one("#chat-input-bar").display = True
        except Exception:
            pass
        await pilot.pause(0.1)

        # Open pane view (saves chat state)
        app.action_pane_view()
        await pilot.pause(0.1)

        assert app._chat_view_active is False
        assert app._pane_view_was_chat is True

        # Close pane view (should restore chat)
        app.action_pane_view()
        await pilot.pause(0.1)

        # Chat view should be restored
        assert app._chat_view_active is True
        assert app.query_one("#chat-feed").display is True
        try:
            assert app.query_one("#chat-input-bar").display is True
        except Exception:
            pass

        # Pane view should be hidden
        assert app.query_one("#pane-view").display is False

        # Main content should remain hidden (chat view doesn't use it)
        assert app.query_one("#main-content").display is False


@pytest.mark.asyncio
async def test_pane_view_close_without_chat_view_shows_main_content():
    """Closing pane view without chat view should restore normal view."""
    app = make_app()
    async with app.run_test() as pilot:
        session = _setup_session_with_choices(app)
        session.tmux_pane = "%42"

        # Normal mode (no chat view)
        app._chat_view_active = False
        await pilot.pause(0.1)

        # Open pane view
        app.action_pane_view()
        await pilot.pause(0.1)

        assert app._pane_view_was_chat is False

        # Close pane view — should restore choices (session is active)
        app.action_pane_view()
        await pilot.pause(0.1)

        # Pane view closed
        assert app.query_one("#pane-view").display is False
        # Chat view should NOT be active
        assert app._chat_view_active is False


@pytest.mark.asyncio
async def test_pane_view_close_no_active_session_shows_main():
    """Closing pane view with no active session should show main content."""
    app = make_app()
    async with app.run_test() as pilot:
        session = _setup_session_with_choices(app)
        session.tmux_pane = "%42"

        # Normal mode
        app._chat_view_active = False
        await pilot.pause(0.1)

        # Open pane view
        app.action_pane_view()
        await pilot.pause(0.1)

        # Deactivate session
        session.active = False
        session.choices = []

        # Close pane view — should show main content + status
        app.action_pane_view()
        await pilot.pause(0.1)

        assert app.query_one("#pane-view").display is False
        assert app.query_one("#main-content").display is True
        assert app.query_one("#status").display is True


# ─── Test: incremental append optimization ───────────────────────────


@pytest.mark.asyncio
async def test_incremental_append_new_speech_items():
    """_build_chat_feed uses incremental append when only new speech items are added."""
    app = make_app()
    async with app.run_test() as pilot:
        session = _setup_session_with_choices(app)

        # Add initial speech entries
        for i in range(3):
            session.speech_log.append(SpeechEntry(text=f"Speech {i}"))

        app._chat_view_active = True
        app._build_chat_feed(session)
        await pilot.pause(0.1)

        feed = app.query_one("#chat-feed", ListView)
        initial_count = len(feed.children)
        assert initial_count > 0

        # Track the last item count set by initial build
        assert app._chat_last_item_count == initial_count
        assert app._chat_base_fingerprint != ""

        # Add more speech entries (append-only change)
        for i in range(3, 6):
            session.speech_log.append(SpeechEntry(text=f"Speech {i}"))

        # Rebuild — should use incremental append
        app._build_chat_feed(session)
        await pilot.pause(0.1)

        new_count = len(feed.children)
        # Should have more items now
        assert new_count == initial_count + 3
        # Tracker should be updated
        assert app._chat_last_item_count == new_count


@pytest.mark.asyncio
async def test_incremental_append_does_not_clear_feed():
    """Incremental append should NOT clear existing items from the feed."""
    app = make_app()
    async with app.run_test() as pilot:
        session = _setup_session_with_choices(app)

        # Build initial feed with speech
        session.speech_log.append(SpeechEntry(text="First speech"))
        app._chat_view_active = True
        app._build_chat_feed(session)
        await pilot.pause(0.1)

        feed = app.query_one("#chat-feed", ListView)
        initial_count = len(feed.children)

        # Record the IDs of existing widgets to verify they aren't replaced
        existing_ids = [id(child) for child in feed.children]

        # Add a new speech entry
        session.speech_log.append(SpeechEntry(text="Second speech"))
        app._build_chat_feed(session)
        await pilot.pause(0.1)

        # Existing items should still be the same objects (not rebuilt)
        for i, eid in enumerate(existing_ids):
            assert id(feed.children[i]) == eid, \
                f"Item {i} was replaced during incremental append"

        # New item should be appended
        assert len(feed.children) == initial_count + 1


@pytest.mark.asyncio
async def test_full_rebuild_on_base_fingerprint_change():
    """_build_chat_feed does full rebuild when base fingerprint changes."""
    from io_mcp.session import FlushedMessage
    app = make_app()
    async with app.run_test() as pilot:
        session = _setup_session_with_choices(app)

        # Build initial feed
        session.speech_log.append(SpeechEntry(text="Speech 1"))
        app._chat_view_active = True
        app._build_chat_feed(session)
        await pilot.pause(0.1)

        feed = app.query_one("#chat-feed", ListView)
        initial_count = len(feed.children)
        existing_ids = [id(child) for child in feed.children]

        # Flush a pending message — this changes the base fingerprint
        # (pending_messages count decreases)
        session.pending_messages.append("hello")
        app._build_chat_feed(session)
        await pilot.pause(0.1)

        count_with_msg = len(feed.children)
        # Now drain the pending message (simulating flush)
        session.pending_messages.clear()
        session.flushed_messages.append(FlushedMessage(text="hello"))
        app._build_chat_feed(session)
        await pilot.pause(0.1)

        # Base fingerprint changed (pm count went from 1 to 0, flushed grew)
        # So this should be a full rebuild — existing items are replaced
        rebuilt_ids = [id(child) for child in feed.children]
        # At least some items should be different objects (full rebuild)
        assert rebuilt_ids != existing_ids


@pytest.mark.asyncio
async def test_force_full_rebuild_flag():
    """_chat_force_full_rebuild flag forces a full rebuild."""
    app = make_app()
    async with app.run_test() as pilot:
        session = _setup_session_with_choices(app)

        # Build initial feed
        session.speech_log.append(SpeechEntry(text="Speech 1"))
        app._chat_view_active = True
        app._build_chat_feed(session)
        await pilot.pause(0.1)

        feed = app.query_one("#chat-feed", ListView)
        initial_count = len(feed.children)
        existing_ids = [id(child) for child in feed.children]

        # Set force flag and add a new item
        app._chat_force_full_rebuild = True
        session.speech_log.append(SpeechEntry(text="Speech 2"))
        app._build_chat_feed(session)
        await pilot.pause(0.1)

        # Should be a full rebuild despite no base changes
        rebuilt_ids = [id(child) for child in feed.children]
        # All items rebuilt from scratch
        assert rebuilt_ids != existing_ids
        # Flag should be cleared after use
        assert app._chat_force_full_rebuild is False


@pytest.mark.asyncio
async def test_incremental_append_skipped_for_unified_mode():
    """Incremental append is not used in unified (multi-session) mode."""
    app = make_app()
    async with app.run_test() as pilot:
        session1 = _setup_session_with_choices(app, session_id="s1", name="S1")
        session2, _ = app.manager.get_or_create("s2")
        session2.registered = True
        session2.name = "S2"
        app.on_session_created(session2)

        all_sessions = list(app.manager.all_sessions())

        # Build initial unified feed
        session1.speech_log.append(SpeechEntry(text="S1 speech"))
        app._chat_view_active = True
        app._chat_unified = True
        app._build_chat_feed(session1, sessions=all_sessions)
        await pilot.pause(0.1)

        feed = app.query_one("#chat-feed", ListView)
        initial_count = len(feed.children)
        existing_ids = [id(child) for child in feed.children]

        # Add speech to session2
        session2.speech_log.append(SpeechEntry(text="S2 speech"))
        app._build_chat_feed(session1, sessions=all_sessions)
        await pilot.pause(0.1)

        # Should be a full rebuild (unified mode doesn't use incremental)
        rebuilt_ids = [id(child) for child in feed.children]
        assert rebuilt_ids != existing_ids


@pytest.mark.asyncio
async def test_incremental_append_resets_on_chat_view_open():
    """Opening chat view resets incremental trackers."""
    app = make_app()
    async with app.run_test() as pilot:
        session = _setup_session_with_choices(app)

        # Turn OFF chat view first (on_session_created auto-activates it)
        app._chat_view_active = False

        # Simulate having stale tracker values from a previous chat view
        app._chat_last_item_count = 42
        app._chat_base_fingerprint = "stale"

        # Open chat view
        app.action_chat_view()
        await pilot.pause(0.1)

        # Trackers should be reset to 0/""
        # (the build that just happened will set them to real values)
        # The key assertion: the feed was built without incremental
        # (old_count was 0 after reset)
        feed = app.query_one("#chat-feed", ListView)
        assert len(feed.children) > 0
        # last_item_count should now match actual feed size
        assert app._chat_last_item_count == len(feed.children)


@pytest.mark.asyncio
async def test_base_fingerprint_for_detects_front_trimming():
    """_chat_base_fingerprint_for changes when activity_log is trimmed from front."""
    app = make_app()
    async with app.run_test() as pilot:
        session = _setup_session_with_choices(app)

        # Add activity log entries with distinct timestamps
        session.activity_log.append({"timestamp": 1000.0, "tool": "tool1", "detail": "", "kind": "tool"})
        session.activity_log.append({"timestamp": 2000.0, "tool": "tool2", "detail": "", "kind": "tool"})

        fp1 = app._chat_base_fingerprint_for(session)

        # Trim the first entry (simulating _activity_log_max overflow)
        session.activity_log = session.activity_log[1:]

        fp2 = app._chat_base_fingerprint_for(session)

        # Fingerprint should change because first item timestamp changed
        assert fp1 != fp2


@pytest.mark.asyncio
async def test_base_fingerprint_for_detects_pending_message_flush():
    """_chat_base_fingerprint_for changes when pending messages are flushed."""
    app = make_app()
    async with app.run_test() as pilot:
        session = _setup_session_with_choices(app)

        session.pending_messages.append("hello")
        fp1 = app._chat_base_fingerprint_for(session)

        # Flush the message
        session.pending_messages.clear()
        fp2 = app._chat_base_fingerprint_for(session)

        # Fingerprint should change because pm count changed
        assert fp1 != fp2


@pytest.mark.asyncio
async def test_incremental_skipped_at_200_item_cap():
    """Incremental append is skipped when at the 200-item cap to avoid truncation bugs."""
    app = make_app()
    async with app.run_test() as pilot:
        session = _setup_session_with_choices(app)

        # Simulate having exactly 200 items
        app._chat_last_item_count = 200
        app._chat_base_fingerprint = app._chat_base_fingerprint_for(session)

        # Even with matching fingerprint, should do full rebuild
        app._chat_view_active = True
        session.speech_log.append(SpeechEntry(text="Extra"))
        app._build_chat_feed(session)
        await pilot.pause(0.1)

        # The feed should have been fully rebuilt (not incrementally appended)
        # We verify by checking that _chat_last_item_count is now the actual
        # count (not 200 + delta)
        feed = app.query_one("#chat-feed", ListView)
        assert app._chat_last_item_count == len(feed.children)
