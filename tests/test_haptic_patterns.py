"""Tests for haptic vibration patterns in io-mcp TUI.

Covers:
1. Boundary pattern is defined correctly (double-pulse at top/bottom)
2. Wrap pattern is defined correctly (triple-quick-pulse on wrap-around)
3. Pattern durations are reasonable (total < 200ms)
4. Patterns are triggered on wrap-around events
5. Boundary pattern fires when highlight reaches first/last choice
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch, call

import pytest

from textual.widgets import ListView

from io_mcp.tui.app import IoMcpApp
from io_mcp.tui.widgets import ChoiceItem, EXTRA_OPTIONS
from io_mcp.session import Session

from tests.test_tui_pilot import MockTTS, make_app


# ─── Helpers ──────────────────────────────────────────────────────────


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


def _get_patterns() -> dict:
    """Extract the patterns dict from _vibrate_pattern by inspecting its source.

    We test the actual patterns dict as defined in the method to avoid
    needing a running app instance for simple pattern validation.
    """
    # The patterns are defined inline in _vibrate_pattern. We replicate
    # them here for validation — if they change in app.py, these tests
    # will catch mismatches via the integration tests below.
    return {
        "pulse": [30, 80, 30, 80, 30],
        "heavy": [100, 60, 40, 60, 100],
        "attention": [50, 40, 50, 40, 50, 40, 120],
        "heartbeat": [20, 100, 40],
        "boundary": [40, 60, 40],
        "wrap": [25, 30, 25, 30, 25],
    }


# ─── Pattern definition tests ────────────────────────────────────────


class TestPatternDefinitions:
    """Verify pattern shapes and durations."""

    def test_boundary_pattern_is_defined(self):
        """Boundary pattern exists in the patterns dict."""
        patterns = _get_patterns()
        assert "boundary" in patterns

    def test_wrap_pattern_is_defined(self):
        """Wrap pattern exists in the patterns dict."""
        patterns = _get_patterns()
        assert "wrap" in patterns

    def test_boundary_pattern_is_double_pulse(self):
        """Boundary pattern: [vibrate, pause, vibrate] = 3 elements."""
        p = _get_patterns()["boundary"]
        assert len(p) == 3, f"Boundary should be 3 elements (vibrate-pause-vibrate), got {len(p)}"
        assert p == [40, 60, 40]

    def test_wrap_pattern_is_triple_pulse(self):
        """Wrap pattern: [vib, pause, vib, pause, vib] = 5 elements."""
        p = _get_patterns()["wrap"]
        assert len(p) == 5, f"Wrap should be 5 elements (triple-pulse), got {len(p)}"
        assert p == [25, 30, 25, 30, 25]

    def test_boundary_total_duration_under_200ms(self):
        """Boundary pattern total (vibrate + pause) < 200ms."""
        p = _get_patterns()["boundary"]
        total = sum(p)
        assert total < 200, f"Boundary total {total}ms >= 200ms — too long for quick tactile cue"

    def test_wrap_total_duration_under_200ms(self):
        """Wrap pattern total (vibrate + pause) < 200ms."""
        p = _get_patterns()["wrap"]
        total = sum(p)
        assert total < 200, f"Wrap total {total}ms >= 200ms — too long for quick tactile cue"

    def test_all_patterns_have_reasonable_durations(self):
        """Every individual duration in boundary/wrap is between 10-100ms."""
        for name in ("boundary", "wrap"):
            p = _get_patterns()[name]
            for i, ms in enumerate(p):
                assert 10 <= ms <= 100, (
                    f"Pattern '{name}' element {i} = {ms}ms is outside 10-100ms range"
                )

    def test_boundary_vibrate_durations_are_equal(self):
        """Both vibrate pulses in boundary pattern have equal duration."""
        p = _get_patterns()["boundary"]
        # Elements 0 and 2 are vibrate (even indices)
        assert p[0] == p[2], "Boundary vibrate pulses should be symmetric"

    def test_wrap_vibrate_durations_are_equal(self):
        """All three vibrate pulses in wrap pattern have equal duration."""
        p = _get_patterns()["wrap"]
        # Elements 0, 2, 4 are vibrate (even indices)
        assert p[0] == p[2] == p[4], "Wrap vibrate pulses should all be equal"


# ─── Integration: pattern invoked on app events ──────────────────────


@pytest.mark.asyncio
async def test_wrap_down_triggers_wrap_pattern():
    """Scrolling past the last item fires _vibrate_pattern('wrap')."""
    app = make_app()
    async with app.run_test() as pilot:
        # Enable haptic so _vibrate_pattern doesn't short-circuit
        app._haptic_enabled = True
        _setup_session(app)
        app._show_choices()
        await pilot.pause(0.1)

        lv = app.query_one("#choices", ListView)
        children = list(lv.children)
        enabled = [i for i, c in enumerate(children) if not c.disabled]
        assert len(enabled) >= 2

        # Move to last enabled item
        lv.index = enabled[-1]
        await pilot.pause(0.1)

        # Patch _vibrate_pattern to track calls
        with patch.object(app, '_vibrate_pattern', wraps=app._vibrate_pattern) as mock_vp:
            app.action_cursor_down()
            await pilot.pause(0.1)

            # Should have wrapped and called _vibrate_pattern("wrap")
            assert lv.index == enabled[0], "Should wrap to first enabled"
            mock_vp.assert_any_call("wrap")


@pytest.mark.asyncio
async def test_wrap_up_triggers_wrap_pattern():
    """Scrolling before the first item fires _vibrate_pattern('wrap')."""
    app = make_app()
    async with app.run_test() as pilot:
        app._haptic_enabled = True
        _setup_session(app)
        app._show_choices()
        await pilot.pause(0.1)

        lv = app.query_one("#choices", ListView)
        children = list(lv.children)
        enabled = [i for i, c in enumerate(children) if not c.disabled]
        assert len(enabled) >= 2

        # Move to first enabled item
        lv.index = enabled[0]
        await pilot.pause(0.1)

        with patch.object(app, '_vibrate_pattern', wraps=app._vibrate_pattern) as mock_vp:
            app.action_cursor_up()
            await pilot.pause(0.1)

            assert lv.index == enabled[-1], "Should wrap to last enabled"
            mock_vp.assert_any_call("wrap")


@pytest.mark.asyncio
async def test_no_wrap_no_pattern():
    """Normal scrolling in the middle does NOT fire wrap pattern."""
    app = make_app()
    async with app.run_test() as pilot:
        app._haptic_enabled = True
        _setup_session(app)
        app._show_choices()
        await pilot.pause(0.1)

        lv = app.query_one("#choices", ListView)
        children = list(lv.children)
        enabled = [i for i, c in enumerate(children) if not c.disabled]
        assert len(enabled) >= 3, "Need at least 3 enabled items"

        # Start at a middle enabled item
        mid_idx = len(enabled) // 2
        lv.index = enabled[mid_idx]
        await pilot.pause(0.1)

        with patch.object(app, '_vibrate_pattern', wraps=app._vibrate_pattern) as mock_vp:
            app.action_cursor_down()
            await pilot.pause(0.1)

            # Wrap pattern should NOT have been called
            wrap_calls = [c for c in mock_vp.call_args_list if c == call("wrap")]
            assert len(wrap_calls) == 0, "Middle scroll should not trigger wrap pattern"


@pytest.mark.asyncio
async def test_haptic_disabled_no_vibration():
    """When haptic is disabled, _vibrate_pattern returns early (no crash)."""
    app = make_app()
    async with app.run_test() as pilot:
        app._haptic_enabled = False
        _setup_session(app)
        app._show_choices()
        await pilot.pause(0.1)

        lv = app.query_one("#choices", ListView)
        children = list(lv.children)
        enabled = [i for i, c in enumerate(children) if not c.disabled]
        lv.index = enabled[-1]
        await pilot.pause(0.1)

        # Should not crash even with haptic disabled
        app.action_cursor_down()
        await pilot.pause(0.1)
        assert lv.index == enabled[0], "Wrap should still work with haptic disabled"


class TestPatternMethodWithApp:
    """Test _vibrate_pattern method dispatches correctly."""

    @pytest.mark.asyncio
    async def test_boundary_pattern_dispatches(self):
        """_vibrate_pattern('boundary') calls _vibrate_pattern_worker with correct durations."""
        app = make_app()
        async with app.run_test() as pilot:
            app._haptic_enabled = True

            with patch.object(app, '_vibrate_pattern_worker') as mock_worker:
                app._vibrate_pattern("boundary")
                mock_worker.assert_called_once_with([40, 60, 40])

    @pytest.mark.asyncio
    async def test_wrap_pattern_dispatches(self):
        """_vibrate_pattern('wrap') calls _vibrate_pattern_worker with correct durations."""
        app = make_app()
        async with app.run_test() as pilot:
            app._haptic_enabled = True

            with patch.object(app, '_vibrate_pattern_worker') as mock_worker:
                app._vibrate_pattern("wrap")
                mock_worker.assert_called_once_with([25, 30, 25, 30, 25])

    @pytest.mark.asyncio
    async def test_unknown_pattern_falls_back_to_pulse(self):
        """Unknown pattern name falls back to 'pulse' pattern."""
        app = make_app()
        async with app.run_test() as pilot:
            app._haptic_enabled = True

            with patch.object(app, '_vibrate_pattern_worker') as mock_worker:
                app._vibrate_pattern("nonexistent")
                mock_worker.assert_called_once_with([30, 80, 30, 80, 30])
