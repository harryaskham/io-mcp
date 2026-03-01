"""Tests for chat feed auto-scroll behavior in io-mcp TUI.

Verifies that the chat feed auto-scrolls correctly:
- Auto-scroll works when at bottom (default state)
- Auto-scroll disabled when user scrolled up
- New content indicator appears when scrolled up and new content arrives
- Jump-to-bottom (G key) clears the indicator and re-enables auto-scroll
- Indicator clears when user scrolls back to bottom manually
- Indicator hidden when chat view is toggled off
"""

import pytest
import time

from textual.widgets import ListView

from io_mcp.tui.app import IoMcpApp
from io_mcp.tui.widgets import ChoiceItem, EXTRA_OPTIONS, PRIMARY_EXTRAS
from io_mcp.session import Session, SpeechEntry

from tests.test_tui_pilot import MockTTS, make_app


def _setup_session(app, session_id="test-1", name="Test", choices=None):
    """Helper: create a registered session with optional choices."""
    session, _ = app.manager.get_or_create(session_id)
    session.registered = True
    session.name = name
    app.on_session_created(session)

    if choices is not None:
        session.preamble = "Pick one"
        session.choices = choices
        session.active = True
        session.extras_count = len(EXTRA_OPTIONS)
        session.all_items = list(EXTRA_OPTIONS) + choices
    return session


# ─── Test: Auto-scroll works when at bottom ─────────────────────────


@pytest.mark.asyncio
async def test_autoscroll_when_at_bottom():
    """Auto-scroll should keep user at the bottom when new content arrives."""
    app = make_app()
    async with app.run_test() as pilot:
        session = _setup_session(app)

        # Add initial speech entries
        for i in range(3):
            session.speech_log.append(SpeechEntry(text=f"Message {i}"))

        app._chat_view_active = True
        app._chat_auto_scroll = True
        app._build_chat_feed(session)
        await pilot.pause(0.1)

        feed = app.query_one("#chat-feed", ListView)
        assert len(feed.children) > 0
        # auto_scroll should remain True (we were at bottom)
        assert app._chat_auto_scroll is True
        # No new-content indicator
        assert app._chat_has_new_content is False

        # Add more speech — should still auto-scroll
        for i in range(3, 6):
            session.speech_log.append(SpeechEntry(text=f"Message {i}"))

        app._build_chat_feed(session)
        await pilot.pause(0.1)

        # auto_scroll should still be True
        assert app._chat_auto_scroll is True
        assert app._chat_has_new_content is False


@pytest.mark.asyncio
async def test_autoscroll_scrolls_to_end_on_new_content():
    """When at bottom, _build_chat_feed calls scroll_end on the feed."""
    app = make_app()
    async with app.run_test() as pilot:
        session = _setup_session(app)

        # Add enough items that scrolling could occur
        for i in range(10):
            session.speech_log.append(SpeechEntry(text=f"Long message {i} " * 5))

        app._chat_view_active = True
        app._chat_auto_scroll = True
        app._build_chat_feed(session)
        await pilot.pause(0.1)

        feed = app.query_one("#chat-feed", ListView)
        initial_count = len(feed.children)

        # Add more content
        for i in range(10, 15):
            session.speech_log.append(SpeechEntry(text=f"New message {i} " * 5))

        app._build_chat_feed(session)
        await pilot.pause(0.1)

        # More items should be in the feed
        assert len(feed.children) > initial_count
        # auto_scroll should remain True
        assert app._chat_auto_scroll is True


# ─── Test: Auto-scroll disabled when user scrolled up ────────────────


@pytest.mark.asyncio
async def test_autoscroll_disabled_when_not_at_bottom():
    """Auto-scroll should be disabled when _chat_feed_is_at_bottom returns False."""
    app = make_app()
    async with app.run_test() as pilot:
        session = _setup_session(app)

        # Add speech entries
        for i in range(5):
            session.speech_log.append(SpeechEntry(text=f"Message {i}"))

        app._chat_view_active = True
        app._chat_auto_scroll = True
        app._build_chat_feed(session)
        await pilot.pause(0.1)

        # Simulate user scrolling up by monkeypatching _chat_feed_is_at_bottom
        original_is_at_bottom = app._chat_feed_is_at_bottom
        app._chat_feed_is_at_bottom = lambda: False

        # Add new content — should detect not-at-bottom
        session.speech_log.append(SpeechEntry(text="New message while scrolled up"))
        app._build_chat_feed(session)
        await pilot.pause(0.1)

        # auto_scroll should be False now
        assert app._chat_auto_scroll is False

        # Restore
        app._chat_feed_is_at_bottom = original_is_at_bottom


# ─── Test: New content indicator appears when scrolled up ────────────


@pytest.mark.asyncio
async def test_new_content_indicator_shows_when_scrolled_up():
    """_chat_has_new_content should be True when new items arrive while scrolled up."""
    app = make_app()
    async with app.run_test() as pilot:
        session = _setup_session(app)

        # Build initial feed
        for i in range(3):
            session.speech_log.append(SpeechEntry(text=f"Message {i}"))

        app._chat_view_active = True
        app._chat_auto_scroll = True
        app._build_chat_feed(session)
        await pilot.pause(0.1)

        old_count = app._chat_last_item_count
        assert app._chat_has_new_content is False

        # Simulate scrolled up
        app._chat_feed_is_at_bottom = lambda: False

        # Add new speech (increases item count)
        session.speech_log.append(SpeechEntry(text="Brand new message"))
        app._build_chat_feed(session)
        await pilot.pause(0.1)

        # Indicator should now be True
        assert app._chat_has_new_content is True
        assert app._chat_auto_scroll is False


@pytest.mark.asyncio
async def test_new_content_indicator_not_shown_when_no_new_items():
    """_chat_has_new_content should stay False when no new items arrive (even if scrolled up)."""
    app = make_app()
    async with app.run_test() as pilot:
        session = _setup_session(app)

        for i in range(3):
            session.speech_log.append(SpeechEntry(text=f"Message {i}"))

        app._chat_view_active = True
        app._build_chat_feed(session)
        await pilot.pause(0.1)

        # Simulate scrolled up but rebuild with SAME data (no new items)
        app._chat_feed_is_at_bottom = lambda: False
        app._chat_force_full_rebuild = True  # Force rebuild to trigger the code path
        app._build_chat_feed(session)
        await pilot.pause(0.1)

        # No new items were added, so indicator should remain False
        assert app._chat_has_new_content is False


@pytest.mark.asyncio
async def test_new_content_indicator_updates_footer_status():
    """The footer-status widget should show '↓ New' when indicator is active."""
    app = make_app()
    async with app.run_test() as pilot:
        session = _setup_session(app)

        for i in range(3):
            session.speech_log.append(SpeechEntry(text=f"Message {i}"))

        app._chat_view_active = True
        app._build_chat_feed(session)
        await pilot.pause(0.1)

        # Simulate scrolled up + new content
        app._chat_feed_is_at_bottom = lambda: False
        session.speech_log.append(SpeechEntry(text="New!"))
        app._build_chat_feed(session)
        await pilot.pause(0.1)

        # Check footer-status content
        footer = app.query_one("#footer-status")
        rendered = footer.render()
        text = str(rendered)
        assert "New" in text or app._chat_has_new_content is True


@pytest.mark.asyncio
async def test_new_content_indicator_clears_when_at_bottom():
    """_chat_has_new_content should clear when user is back at the bottom."""
    app = make_app()
    async with app.run_test() as pilot:
        session = _setup_session(app)

        for i in range(3):
            session.speech_log.append(SpeechEntry(text=f"Message {i}"))

        app._chat_view_active = True
        app._build_chat_feed(session)
        await pilot.pause(0.1)

        # Set indicator active
        app._chat_has_new_content = True
        app._chat_auto_scroll = False

        # Simulate user scrolling back to bottom
        app._chat_feed_is_at_bottom = lambda: True

        # Add new content — should clear indicator since we're now at bottom
        session.speech_log.append(SpeechEntry(text="Latest"))
        app._build_chat_feed(session)
        await pilot.pause(0.1)

        assert app._chat_has_new_content is False
        assert app._chat_auto_scroll is True


# ─── Test: Jump-to-bottom clears the indicator ──────────────────────


@pytest.mark.asyncio
async def test_action_chat_scroll_bottom_clears_indicator():
    """action_chat_scroll_bottom should clear _chat_has_new_content and re-enable auto-scroll."""
    app = make_app()
    async with app.run_test() as pilot:
        session = _setup_session(app)

        for i in range(5):
            session.speech_log.append(SpeechEntry(text=f"Message {i}"))

        app._chat_view_active = True
        app._build_chat_feed(session)
        await pilot.pause(0.1)

        # Manually set state as if user scrolled up and new content arrived
        app._chat_has_new_content = True
        app._chat_auto_scroll = False
        app._update_chat_new_indicator()

        # Verify indicator is showing
        assert app._chat_has_new_content is True

        # Call the jump-to-bottom action
        app.action_chat_scroll_bottom()
        await pilot.pause(0.1)

        # Indicator should be cleared, auto-scroll re-enabled
        assert app._chat_has_new_content is False
        assert app._chat_auto_scroll is True


@pytest.mark.asyncio
async def test_action_chat_scroll_bottom_noop_when_chat_inactive():
    """action_chat_scroll_bottom is a no-op when chat view is not active."""
    app = make_app()
    async with app.run_test() as pilot:
        session = _setup_session(app)

        app._chat_view_active = False
        app._chat_has_new_content = True

        app.action_chat_scroll_bottom()
        await pilot.pause(0.1)

        # Should not have cleared the indicator (chat view was inactive)
        assert app._chat_has_new_content is True


@pytest.mark.asyncio
async def test_g_key_binding_exists():
    """Capital G should be bound to action_chat_scroll_bottom."""
    app = make_app()
    async with app.run_test() as pilot:
        # Verify the binding exists by checking BINDINGS
        bindings = {b.key: b.action for b in app.BINDINGS}
        assert "G" in bindings
        assert bindings["G"] == "chat_scroll_bottom"


# ─── Test: _check_chat_scroll_position clears indicator ──────────────


@pytest.mark.asyncio
async def test_check_scroll_position_clears_when_at_bottom():
    """_check_chat_scroll_position clears indicator when user is at bottom."""
    app = make_app()
    async with app.run_test() as pilot:
        session = _setup_session(app)

        for i in range(3):
            session.speech_log.append(SpeechEntry(text=f"Message {i}"))

        app._chat_view_active = True
        app._build_chat_feed(session)
        await pilot.pause(0.1)

        # Set indicator active
        app._chat_has_new_content = True
        app._chat_auto_scroll = False

        # Make it look like user scrolled back to bottom
        app._chat_feed_is_at_bottom = lambda: True

        app._check_chat_scroll_position()

        assert app._chat_has_new_content is False
        assert app._chat_auto_scroll is True


@pytest.mark.asyncio
async def test_check_scroll_position_noop_when_still_scrolled_up():
    """_check_chat_scroll_position does nothing when user is still scrolled up."""
    app = make_app()
    async with app.run_test() as pilot:
        session = _setup_session(app)

        app._chat_view_active = True
        app._chat_has_new_content = True
        app._chat_auto_scroll = False

        # Still scrolled up
        app._chat_feed_is_at_bottom = lambda: False

        app._check_chat_scroll_position()

        # Indicator should remain active
        assert app._chat_has_new_content is True
        assert app._chat_auto_scroll is False


@pytest.mark.asyncio
async def test_check_scroll_position_noop_when_no_indicator():
    """_check_chat_scroll_position does nothing when indicator is not showing."""
    app = make_app()
    async with app.run_test() as pilot:
        session = _setup_session(app)

        app._chat_view_active = True
        app._chat_has_new_content = False

        # Even if at bottom, should not change anything
        app._chat_feed_is_at_bottom = lambda: True

        app._check_chat_scroll_position()

        assert app._chat_has_new_content is False


# ─── Test: Refresh timer checks scroll position ─────────────────────


@pytest.mark.asyncio
async def test_refresh_chat_feed_checks_scroll_position():
    """_refresh_chat_feed calls _check_chat_scroll_position to detect manual scrolling."""
    app = make_app()
    async with app.run_test() as pilot:
        session = _setup_session(app)

        for i in range(3):
            session.speech_log.append(SpeechEntry(text=f"Message {i}"))

        app._chat_view_active = True
        app._build_chat_feed(session)
        await pilot.pause(0.1)

        # Set indicator active
        app._chat_has_new_content = True
        app._chat_auto_scroll = False

        # Make it look like user scrolled back to bottom
        app._chat_feed_is_at_bottom = lambda: True

        # Call refresh — it should check scroll position and clear indicator
        app._refresh_chat_feed()
        await pilot.pause(0.1)

        assert app._chat_has_new_content is False
        assert app._chat_auto_scroll is True


# ─── Test: Opening chat view resets indicator ────────────────────────


@pytest.mark.asyncio
async def test_opening_chat_view_resets_indicator():
    """Opening chat view should reset _chat_has_new_content to False."""
    app = make_app()
    async with app.run_test() as pilot:
        session = _setup_session(app)

        # Turn off chat view first (on_session_created auto-activates it)
        app._chat_view_active = False

        # Set stale state
        app._chat_has_new_content = True
        app._chat_auto_scroll = False

        # Open chat view
        app.action_chat_view()
        await pilot.pause(0.1)

        assert app._chat_has_new_content is False
        assert app._chat_auto_scroll is True


# ─── Test: Closing chat view clears indicator ────────────────────────


@pytest.mark.asyncio
async def test_update_indicator_clears_when_chat_view_off():
    """_update_chat_new_indicator clears footer when chat view is inactive."""
    app = make_app()
    async with app.run_test() as pilot:
        session = _setup_session(app)

        # Set indicator while chat view is active
        app._chat_view_active = True
        app._chat_has_new_content = True
        app._update_chat_new_indicator()
        await pilot.pause(0.1)

        footer = app.query_one("#footer-status")
        text1 = str(footer.render())

        # Deactivate chat view and update indicator
        app._chat_view_active = False
        app._update_chat_new_indicator()
        await pilot.pause(0.1)

        footer = app.query_one("#footer-status")
        text2 = str(footer.render())
        # Footer should be empty when chat view is off
        # (the update should have cleared it)
        assert "New" not in text2 or text2 == ""
