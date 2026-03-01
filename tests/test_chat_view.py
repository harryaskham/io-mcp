"""Tests for chat view layout behavior in io-mcp TUI.

Verifies that the chat view guards and layout logic work correctly:
- _populate_chat_choices_list populates #chat-choices correctly
- _show_waiting hides #chat-choices in chat view mode
- _show_choices delegates to _populate_chat_choices_list in chat view
- _show_session_waiting returns early in chat view mode
"""

import pytest

from textual.widgets import ListView

from io_mcp.tui.app import IoMcpApp
from io_mcp.tui.widgets import ChoiceItem, EXTRA_OPTIONS, PRIMARY_EXTRAS
from io_mcp.session import Session


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
