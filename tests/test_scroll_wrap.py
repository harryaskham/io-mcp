"""Tests for scroll wrap-around behavior in io-mcp TUI.

When the user scrolls past the last item, the cursor should wrap to the
first item, and vice versa.  This is critical for the scroll-wheel-only
interface (smart ring) where reversing direction is cumbersome.

Covers:
1. Wrap down: at last item → cursor moves to first enabled item
2. Wrap up: at first item → cursor moves to last enabled item
3. Disabled items (PreambleItem) are skipped during wrap
4. Normal scrolling in the middle is unaffected (delegates to ListView)
5. Round-trip wrap (down then up returns to original)
6. Input mode blocks scrolling
"""

from __future__ import annotations

import pytest
import threading

from textual.widgets import ListView

from io_mcp.tui.app import IoMcpApp
from io_mcp.tui.widgets import ChoiceItem, EXTRA_OPTIONS
from io_mcp.session import Session

from tests.test_tui_pilot import MockTTS, make_app


# ─── Helper ──────────────────────────────────────────────────────────


def _setup_session(app, session_id="test-1", name="Test", choices=None):
    """Create a registered session with active choices."""
    session, _ = app.manager.get_or_create(session_id)
    session.registered = True
    session.name = name
    app.on_session_created(session)

    # Disable chat view so _show_choices populates #choices ListView
    app._chat_view_active = False

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
    app._show_choices()
    return session


# ─── Tests ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_wrap_down_at_last_item():
    """Scrolling down at the last item wraps to the first enabled item."""
    app = make_app()
    async with app.run_test() as pilot:
        _setup_session(app)
        app._show_choices()
        await pilot.pause(0.1)

        lv = app.query_one("#choices", ListView)
        children = list(lv.children)
        assert len(children) > 0

        # Find last and first enabled indices
        enabled = [i for i, c in enumerate(children) if not c.disabled]
        assert len(enabled) >= 2, "Need at least 2 enabled items"
        first_enabled = enabled[0]
        last_enabled = enabled[-1]

        # Move to the last enabled item
        lv.index = last_enabled
        await pilot.pause(0.1)
        assert lv.index == last_enabled

        # Scroll down — should wrap to first enabled
        app.action_cursor_down()
        await pilot.pause(0.1)
        assert lv.index == first_enabled, (
            f"Expected wrap to {first_enabled}, got {lv.index}"
        )


@pytest.mark.asyncio
async def test_wrap_up_at_first_item():
    """Scrolling up at the first enabled item wraps to the last enabled item."""
    app = make_app()
    async with app.run_test() as pilot:
        _setup_session(app)
        app._show_choices()
        await pilot.pause(0.1)

        lv = app.query_one("#choices", ListView)
        children = list(lv.children)
        enabled = [i for i, c in enumerate(children) if not c.disabled]
        assert len(enabled) >= 2
        first_enabled = enabled[0]
        last_enabled = enabled[-1]

        # Move to first enabled item
        lv.index = first_enabled
        await pilot.pause(0.1)
        assert lv.index == first_enabled

        # Scroll up — should wrap to last enabled
        app.action_cursor_up()
        await pilot.pause(0.1)
        assert lv.index == last_enabled, (
            f"Expected wrap to {last_enabled}, got {lv.index}"
        )


@pytest.mark.asyncio
async def test_no_wrap_in_middle():
    """Scrolling in the middle of the list does NOT wrap — normal navigation."""
    app = make_app()
    async with app.run_test() as pilot:
        _setup_session(app)
        app._show_choices()
        await pilot.pause(0.1)

        lv = app.query_one("#choices", ListView)
        children = list(lv.children)
        enabled = [i for i, c in enumerate(children) if not c.disabled]
        assert len(enabled) >= 3, "Need at least 3 enabled items for middle test"

        # Start at a middle enabled item
        mid_idx = len(enabled) // 2
        mid = enabled[mid_idx]
        lv.index = mid
        await pilot.pause(0.1)

        # Scroll down — should advance normally, not wrap
        app.action_cursor_down()
        await pilot.pause(0.1)
        # Should move to next enabled item, not wrap to first
        assert lv.index != enabled[0], (
            f"Middle scroll should not wrap to first. index={lv.index}, mid={mid}"
        )
        assert lv.index > mid, (
            f"Should advance past mid={mid}, got {lv.index}"
        )


@pytest.mark.asyncio
async def test_wrap_skips_disabled_items():
    """Wrap-around skips disabled items (e.g. PreambleItem)."""
    app = make_app()
    async with app.run_test() as pilot:
        _setup_session(app)
        app._show_choices()
        await pilot.pause(0.1)

        lv = app.query_one("#choices", ListView)
        children = list(lv.children)
        enabled = [i for i, c in enumerate(children) if not c.disabled]

        assert len(enabled) >= 2, "Need enabled items"

        # Place cursor at last enabled, scroll down
        lv.index = enabled[-1]
        await pilot.pause(0.1)
        app.action_cursor_down()
        await pilot.pause(0.1)

        # Should wrap to first ENABLED, never to a disabled item
        assert lv.index == enabled[0]
        assert not children[lv.index].disabled


@pytest.mark.asyncio
async def test_wrap_down_then_up_roundtrip():
    """Wrapping down then back up returns to original position."""
    app = make_app()
    async with app.run_test() as pilot:
        _setup_session(app)
        app._show_choices()
        await pilot.pause(0.1)

        lv = app.query_one("#choices", ListView)
        children = list(lv.children)
        enabled = [i for i, c in enumerate(children) if not c.disabled]
        last_enabled = enabled[-1]

        # Start at last enabled
        lv.index = last_enabled
        await pilot.pause(0.1)

        # Wrap down → first
        app.action_cursor_down()
        await pilot.pause(0.1)
        assert lv.index == enabled[0]

        # Wrap up → back to last
        app.action_cursor_up()
        await pilot.pause(0.1)
        assert lv.index == last_enabled


@pytest.mark.asyncio
async def test_input_mode_blocks_scroll():
    """Scroll does nothing when session is in input_mode."""
    app = make_app()
    async with app.run_test() as pilot:
        session = _setup_session(app)
        app._show_choices()
        await pilot.pause(0.1)

        lv = app.query_one("#choices", ListView)
        enabled = [i for i, c in enumerate(list(lv.children)) if not c.disabled]
        lv.index = enabled[-1]
        await pilot.pause(0.1)
        original = lv.index

        # Enable input mode — scroll should be blocked
        session.input_mode = True
        app.action_cursor_down()
        await pilot.pause(0.1)
        assert lv.index == original, "Scroll should be blocked in input_mode"
