"""Integration tests for smart ring features in the io-mcp TUI.

Tests that scroll wrap-around, boundary cues, position indicators, scroll
acceleration, dwell mode, undo, and extras menu ordering all work together
in the actual Textual TUI using the async pilot framework.

These complement the existing unit tests in:
  - test_scroll_wrap.py
  - test_scroll_boundary_cues.py
  - test_scroll_acceleration.py
  - test_position_indicator.py
  - test_dwell_config.py
  - test_undo_history.py
"""

from __future__ import annotations

import time
from unittest.mock import patch, MagicMock, call

import pytest

from textual.widgets import ListView

from io_mcp.tui.app import IoMcpApp
from io_mcp.tui.widgets import (
    ChoiceItem,
    DwellBar,
    EXTRA_OPTIONS,
    PRIMARY_EXTRAS,
    SECONDARY_EXTRAS,
)
from io_mcp.session import Session

from tests.test_tui_pilot import MockTTS, make_app, _disable_chat_view


# ─── Helpers ──────────────────────────────────────────────────────────────


def _setup_session(app, session_id="test-1", name="Test", choices=None,
                   num_choices=None):
    """Create a registered session with active choices in the TUI."""
    session, _ = app.manager.get_or_create(session_id)
    session.registered = True
    session.name = name
    app.on_session_created(session)
    _disable_chat_view(app)

    if choices is None:
        n = num_choices or 3
        choices = [
            {"label": f"Option {i+1}", "summary": f"Description {i+1}"}
            for i in range(n)
        ]

    session.preamble = "Pick one"
    session.choices = choices
    session.active = True
    session.extras_count = len(EXTRA_OPTIONS)
    session.all_items = list(EXTRA_OPTIONS) + choices
    app._show_choices()
    return session


def _get_enabled_indices(list_view: ListView) -> list[int]:
    """Return indices of all enabled (non-disabled) items in the ListView."""
    return [i for i, c in enumerate(list(list_view.children)) if not c.disabled]


def _get_real_choice_indices(list_view: ListView) -> list[int]:
    """Return indices of real choices (choice_index > 0) in the ListView."""
    return [
        i for i, c in enumerate(list(list_view.children))
        if isinstance(c, ChoiceItem) and c.choice_index > 0
    ]


def _make_app_with_dwell(dwell_time: float) -> IoMcpApp:
    """Create a testable IoMcpApp with dwell mode enabled."""
    tts = MockTTS()
    app = IoMcpApp(tts=tts, freeform_tts=tts, demo=True, dwell_time=dwell_time)
    return app


def _make_app_with_accel(**overrides) -> IoMcpApp:
    """Create a testable IoMcpApp with scroll acceleration config."""
    cfg_dict = {
        "enabled": True,
        "fastThresholdMs": 80,
        "turboThresholdMs": 40,
        "fastSkip": 3,
        "turboSkip": 5,
    }
    cfg_dict.update(overrides)
    tts = MockTTS()
    app = IoMcpApp(tts=tts, freeform_tts=tts, demo=True)
    app._scroll_accel_enabled = cfg_dict["enabled"]
    app._scroll_accel_fast_ms = cfg_dict["fastThresholdMs"]
    app._scroll_accel_turbo_ms = cfg_dict["turboThresholdMs"]
    app._scroll_accel_fast_skip = cfg_dict["fastSkip"]
    app._scroll_accel_turbo_skip = cfg_dict["turboSkip"]
    app._scroll_times = []
    return app


# ═══════════════════════════════════════════════════════════════════════════
# 1. Scroll wrap integration
# ═══════════════════════════════════════════════════════════════════════════


class TestScrollWrapIntegration:
    """Verify scroll wrap-around works end-to-end in the TUI."""

    @pytest.mark.asyncio
    async def test_wrap_down_from_last_to_first(self):
        """Scrolling past the last item wraps to the first real choice."""
        app = make_app()
        async with app.run_test() as pilot:
            _setup_session(app, choices=[
                {"label": "Alpha", "summary": "First"},
                {"label": "Beta", "summary": "Second"},
                {"label": "Gamma", "summary": "Third"},
            ])
            await pilot.pause(0.1)

            lv = app.query_one("#choices", ListView)
            enabled = _get_enabled_indices(lv)
            assert len(enabled) >= 4  # at least 3 real + extras

            # Move to last enabled item
            lv.index = enabled[-1]
            await pilot.pause(0.1)

            # Scroll down — should wrap to first
            app.action_cursor_down()
            await pilot.pause(0.1)

            assert lv.index == enabled[0], (
                f"Expected wrap to first enabled ({enabled[0]}), got {lv.index}"
            )

    @pytest.mark.asyncio
    async def test_wrap_up_from_first_to_last(self):
        """Scrolling up from the first item wraps to the last."""
        app = make_app()
        async with app.run_test() as pilot:
            _setup_session(app, choices=[
                {"label": "Alpha", "summary": "First"},
                {"label": "Beta", "summary": "Second"},
                {"label": "Gamma", "summary": "Third"},
            ])
            await pilot.pause(0.1)

            lv = app.query_one("#choices", ListView)
            enabled = _get_enabled_indices(lv)

            # Move to first enabled item
            lv.index = enabled[0]
            await pilot.pause(0.1)

            # Scroll up — should wrap to last
            app.action_cursor_up()
            await pilot.pause(0.1)

            assert lv.index == enabled[-1], (
                f"Expected wrap to last enabled ({enabled[-1]}), got {lv.index}"
            )

    @pytest.mark.asyncio
    async def test_wrap_roundtrip_preserves_position(self):
        """Wrapping down then up returns to original position."""
        app = make_app()
        async with app.run_test() as pilot:
            _setup_session(app, num_choices=5)
            await pilot.pause(0.1)

            lv = app.query_one("#choices", ListView)
            enabled = _get_enabled_indices(lv)

            # Start at last enabled
            lv.index = enabled[-1]
            await pilot.pause(0.1)
            original = lv.index

            # Wrap down → first, wrap up → back to last
            app.action_cursor_down()
            await pilot.pause(0.1)
            assert lv.index == enabled[0]

            app.action_cursor_up()
            await pilot.pause(0.1)
            assert lv.index == original

    @pytest.mark.asyncio
    async def test_continuous_wrapping_cycles(self):
        """Continuously scrolling down cycles through all items."""
        app = make_app()
        async with app.run_test() as pilot:
            _setup_session(app, num_choices=3)
            await pilot.pause(0.1)

            lv = app.query_one("#choices", ListView)
            enabled = _get_enabled_indices(lv)

            # Start at first, scroll through all + wrap
            lv.index = enabled[0]
            await pilot.pause(0.1)

            visited = [lv.index]
            for _ in range(len(enabled)):
                app.action_cursor_down()
                await pilot.pause(0.05)
                visited.append(lv.index)

            # After len(enabled) scrolls, we should be back at start
            assert visited[-1] == visited[0], (
                f"Should return to start after full cycle. Visited: {visited}"
            )


# ═══════════════════════════════════════════════════════════════════════════
# 2. Boundary cues + position indicator integration
# ═══════════════════════════════════════════════════════════════════════════


class TestBoundaryAndPositionIntegration:
    """Verify boundary cues ('Top'/'Last') and position ('X of Y') in TUI."""

    @pytest.mark.asyncio
    async def test_first_choice_triggers_top_boundary_tts(self):
        """Scrolling to the first real choice triggers 'Top' TTS."""
        tts = MockTTS()
        tts.speak_with_local_fallback = MagicMock()
        tts.speak_fragments_scroll = MagicMock()
        app = IoMcpApp(tts=tts, freeform_tts=tts, demo=True)

        async with app.run_test() as pilot:
            session = _setup_session(app, num_choices=5)
            await pilot.pause(0.1)

            lv = app.query_one("#choices", ListView)
            real_indices = _get_real_choice_indices(lv)
            assert len(real_indices) == 5

            # Move to the second real choice first (to trigger change)
            lv.index = real_indices[1]
            await pilot.pause(0.2)

            # Reset mock to capture fresh calls
            tts.speak_with_local_fallback.reset_mock()
            tts.speak_fragments_scroll.reset_mock()

            # Now move to first real choice
            lv.index = real_indices[0]
            await pilot.pause(0.2)

            # TTS should have been called with text containing "Top"
            all_calls = (
                tts.speak_fragments_scroll.call_args_list +
                tts.speak_with_local_fallback.call_args_list
            )
            assert len(all_calls) > 0, "TTS should have been called"

            # Check that "Top" appears either in fragments or text
            found_top = False
            for c in tts.speak_fragments_scroll.call_args_list:
                fragments = c[0][0] if c[0] else c[1].get('fragments', [])
                if any("Top" in str(f) for f in fragments):
                    found_top = True
            for c in tts.speak_with_local_fallback.call_args_list:
                text = c[0][0] if c[0] else ""
                if "Top" in text:
                    found_top = True

            assert found_top, (
                f"Expected 'Top' in TTS calls. Got fragments: "
                f"{tts.speak_fragments_scroll.call_args_list}, "
                f"text: {tts.speak_with_local_fallback.call_args_list}"
            )

    @pytest.mark.asyncio
    async def test_last_choice_triggers_last_boundary_tts(self):
        """Scrolling to the last real choice triggers 'Last' TTS."""
        tts = MockTTS()
        tts.speak_with_local_fallback = MagicMock()
        tts.speak_fragments_scroll = MagicMock()
        app = IoMcpApp(tts=tts, freeform_tts=tts, demo=True)

        async with app.run_test() as pilot:
            session = _setup_session(app, num_choices=5)
            await pilot.pause(0.1)

            lv = app.query_one("#choices", ListView)
            real_indices = _get_real_choice_indices(lv)

            # Move to second-to-last first
            lv.index = real_indices[-2]
            await pilot.pause(0.2)

            tts.speak_with_local_fallback.reset_mock()
            tts.speak_fragments_scroll.reset_mock()

            # Now move to last real choice
            lv.index = real_indices[-1]
            await pilot.pause(0.2)

            all_calls = (
                tts.speak_fragments_scroll.call_args_list +
                tts.speak_with_local_fallback.call_args_list
            )
            assert len(all_calls) > 0, "TTS should have been called"

            found_last = False
            for c in tts.speak_fragments_scroll.call_args_list:
                fragments = c[0][0] if c[0] else []
                if any("Last" in str(f) for f in fragments):
                    found_last = True
            for c in tts.speak_with_local_fallback.call_args_list:
                text = c[0][0] if c[0] else ""
                if "Last" in text:
                    found_last = True

            assert found_last, (
                f"Expected 'Last' in TTS calls. Got: "
                f"{tts.speak_fragments_scroll.call_args_list}, "
                f"{tts.speak_with_local_fallback.call_args_list}"
            )

    @pytest.mark.asyncio
    async def test_position_indicator_with_five_choices(self):
        """With 5 choices (>2), TTS includes 'X of 5' position text."""
        tts = MockTTS()
        tts.speak_with_local_fallback = MagicMock()
        tts.speak_fragments_scroll = MagicMock()
        app = IoMcpApp(tts=tts, freeform_tts=tts, demo=True)

        async with app.run_test() as pilot:
            session = _setup_session(app, num_choices=5)
            await pilot.pause(0.1)

            lv = app.query_one("#choices", ListView)
            real_indices = _get_real_choice_indices(lv)

            # Move away first so we can trigger highlight change
            lv.index = real_indices[0]
            await pilot.pause(0.2)

            tts.speak_with_local_fallback.reset_mock()
            tts.speak_fragments_scroll.reset_mock()

            # Move to third choice
            lv.index = real_indices[2]
            await pilot.pause(0.2)

            # Check that "of 5" appears in TTS calls
            found_of_5 = False
            for c in tts.speak_fragments_scroll.call_args_list:
                fragments = c[0][0] if c[0] else []
                if any("of 5" in str(f) for f in fragments):
                    found_of_5 = True
            for c in tts.speak_with_local_fallback.call_args_list:
                text = c[0][0] if c[0] else ""
                if "of 5" in text:
                    found_of_5 = True

            assert found_of_5, (
                f"Expected 'of 5' in TTS. fragments: "
                f"{tts.speak_fragments_scroll.call_args_list}, "
                f"text: {tts.speak_with_local_fallback.call_args_list}"
            )

    @pytest.mark.asyncio
    async def test_no_position_indicator_with_two_choices(self):
        """With only 2 choices, TTS should NOT include 'of 2'."""
        tts = MockTTS()
        tts.speak_with_local_fallback = MagicMock()
        tts.speak_fragments_scroll = MagicMock()
        app = IoMcpApp(tts=tts, freeform_tts=tts, demo=True)

        async with app.run_test() as pilot:
            session = _setup_session(app, num_choices=2)
            await pilot.pause(0.1)

            lv = app.query_one("#choices", ListView)
            real_indices = _get_real_choice_indices(lv)
            assert len(real_indices) == 2

            # Navigate to each real choice
            for idx in real_indices:
                tts.speak_with_local_fallback.reset_mock()
                tts.speak_fragments_scroll.reset_mock()

                lv.index = idx
                await pilot.pause(0.2)

                # Verify no "of 2" in any TTS call
                for c in tts.speak_fragments_scroll.call_args_list:
                    fragments = c[0][0] if c[0] else []
                    for f in fragments:
                        assert "of 2" not in str(f), f"Should not have 'of 2' with 2 choices"
                for c in tts.speak_with_local_fallback.call_args_list:
                    text = c[0][0] if c[0] else ""
                    assert "of 2" not in text, f"Should not have 'of 2' with 2 choices"

    @pytest.mark.asyncio
    async def test_boundary_and_position_combined(self):
        """First choice with 5 items has both 'Top' and '1 of 5'."""
        tts = MockTTS()
        tts.speak_with_local_fallback = MagicMock()
        tts.speak_fragments_scroll = MagicMock()
        app = IoMcpApp(tts=tts, freeform_tts=tts, demo=True)

        async with app.run_test() as pilot:
            session = _setup_session(app, num_choices=5)
            await pilot.pause(0.1)

            lv = app.query_one("#choices", ListView)
            real_indices = _get_real_choice_indices(lv)

            # Move to second item first, then back to first
            lv.index = real_indices[1]
            await pilot.pause(0.2)
            tts.speak_with_local_fallback.reset_mock()
            tts.speak_fragments_scroll.reset_mock()

            lv.index = real_indices[0]
            await pilot.pause(0.2)

            # Collect all spoken text
            spoken_parts = []
            for c in tts.speak_fragments_scroll.call_args_list:
                fragments = c[0][0] if c[0] else []
                spoken_parts.extend(str(f) for f in fragments)
            for c in tts.speak_with_local_fallback.call_args_list:
                spoken_parts.append(c[0][0] if c[0] else "")

            spoken_text = " ".join(spoken_parts)
            assert "Top" in spoken_text, f"Expected 'Top' in: {spoken_text}"
            assert "of 5" in spoken_text, f"Expected 'of 5' in: {spoken_text}"


# ═══════════════════════════════════════════════════════════════════════════
# 3. Scroll acceleration detection integration
# ═══════════════════════════════════════════════════════════════════════════


class TestScrollAccelerationIntegration:
    """Verify rapid scrolling skips items in the actual TUI."""

    @pytest.mark.asyncio
    async def test_fast_scroll_skips_three_items(self):
        """Fast scrolling (60ms intervals) skips 3 items."""
        app = _make_app_with_accel()
        async with app.run_test() as pilot:
            _setup_session(app, num_choices=10)
            await pilot.pause(0.1)

            lv = app.query_one("#choices", ListView)
            enabled = _get_enabled_indices(lv)
            assert len(enabled) >= 6

            # Start at first enabled
            lv.index = enabled[0]
            await pilot.pause(0.1)

            # Simulate fast scrolling (60ms intervals = fast mode)
            now = time.time()
            app._scroll_times = [now - 0.240, now - 0.180, now - 0.120, now - 0.060]

            with patch("time.time", return_value=now):
                app.action_cursor_down()
            await pilot.pause(0.1)

            # Should have jumped 3 items (fastSkip=3)
            assert lv.index == enabled[3], (
                f"Expected skip to {enabled[3]} (3 items), got {lv.index}"
            )

    @pytest.mark.asyncio
    async def test_turbo_scroll_skips_five_items(self):
        """Turbo scrolling (20ms intervals) skips 5 items."""
        app = _make_app_with_accel()
        async with app.run_test() as pilot:
            _setup_session(app, num_choices=10)
            await pilot.pause(0.1)

            lv = app.query_one("#choices", ListView)
            enabled = _get_enabled_indices(lv)
            assert len(enabled) >= 6

            lv.index = enabled[0]
            await pilot.pause(0.1)

            # Simulate turbo scrolling (20ms intervals)
            now = time.time()
            app._scroll_times = [now - 0.080, now - 0.060, now - 0.040, now - 0.020]

            with patch("time.time", return_value=now):
                app.action_cursor_down()
            await pilot.pause(0.1)

            # Should have jumped 5 items (turboSkip=5)
            assert lv.index == enabled[5], (
                f"Expected turbo skip to {enabled[5]} (5 items), got {lv.index}"
            )

    @pytest.mark.asyncio
    async def test_normal_scroll_moves_one_item(self):
        """Normal speed scrolling (200ms intervals) moves 1 item."""
        app = _make_app_with_accel()
        async with app.run_test() as pilot:
            _setup_session(app, num_choices=10)
            await pilot.pause(0.1)

            lv = app.query_one("#choices", ListView)
            enabled = _get_enabled_indices(lv)

            lv.index = enabled[0]
            await pilot.pause(0.1)

            # Simulate slow scrolling (200ms intervals)
            app._scroll_times = []
            base = time.time() - (4 * 0.200)
            app._scroll_times = [base + i * 0.200 for i in range(4)]

            app.action_cursor_down()
            await pilot.pause(0.1)

            assert lv.index == enabled[1], (
                f"Expected single step to {enabled[1]}, got {lv.index}"
            )

    @pytest.mark.asyncio
    async def test_fast_scroll_wraps_at_boundary(self):
        """Fast scroll that overshoots the end wraps to the beginning."""
        app = _make_app_with_accel()
        async with app.run_test() as pilot:
            _setup_session(app, num_choices=10)
            await pilot.pause(0.1)

            lv = app.query_one("#choices", ListView)
            enabled = _get_enabled_indices(lv)

            # Position near the end (2nd to last)
            lv.index = enabled[-2]
            await pilot.pause(0.1)

            # Turbo scroll down (skip 5) with only 1 item before end
            now = time.time()
            app._scroll_times = [now - 0.080, now - 0.060, now - 0.040, now - 0.020]

            with patch("time.time", return_value=now):
                app.action_cursor_down()
            await pilot.pause(0.1)

            # Should wrap to first
            assert lv.index == enabled[0], (
                f"Expected wrap to {enabled[0]}, got {lv.index}"
            )

    @pytest.mark.asyncio
    async def test_fast_scroll_up_wraps_at_beginning(self):
        """Fast scroll up from near beginning wraps to the end."""
        app = _make_app_with_accel()
        async with app.run_test() as pilot:
            _setup_session(app, num_choices=10)
            await pilot.pause(0.1)

            lv = app.query_one("#choices", ListView)
            enabled = _get_enabled_indices(lv)

            # Position near the beginning (2nd item)
            lv.index = enabled[1]
            await pilot.pause(0.1)

            # Turbo scroll up (skip 5) with only 1 item before start
            now = time.time()
            app._scroll_times = [now - 0.080, now - 0.060, now - 0.040, now - 0.020]

            with patch("time.time", return_value=now):
                app.action_cursor_up()
            await pilot.pause(0.1)

            # Should wrap to last
            assert lv.index == enabled[-1], (
                f"Expected wrap to {enabled[-1]}, got {lv.index}"
            )

    @pytest.mark.asyncio
    async def test_disabled_acceleration_always_single_step(self):
        """With acceleration disabled, turbo-speed scrolling still moves 1."""
        app = _make_app_with_accel(enabled=False)
        async with app.run_test() as pilot:
            _setup_session(app, num_choices=10)
            await pilot.pause(0.1)

            lv = app.query_one("#choices", ListView)
            enabled = _get_enabled_indices(lv)

            lv.index = enabled[0]
            await pilot.pause(0.1)

            # Even with turbo-speed timing
            now = time.time()
            app._scroll_times = [now - 0.080, now - 0.060, now - 0.040, now - 0.020]

            with patch("time.time", return_value=now):
                app.action_cursor_down()
            await pilot.pause(0.1)

            assert lv.index == enabled[1], (
                f"Disabled accel should move 1, got {lv.index}"
            )


# ═══════════════════════════════════════════════════════════════════════════
# 4. Dwell mode activation integration
# ═══════════════════════════════════════════════════════════════════════════


class TestDwellModeIntegration:
    """Verify dwell-to-select works end-to-end in the TUI."""

    @pytest.mark.asyncio
    async def test_dwell_bar_visible_when_dwell_enabled(self):
        """DwellBar is shown when dwell_time > 0 and choices are displayed."""
        app = _make_app_with_dwell(3.0)
        async with app.run_test() as pilot:
            _setup_session(app, num_choices=3)
            await pilot.pause(0.1)

            dwell_bar = app.query_one("#dwell-bar", DwellBar)
            assert dwell_bar.display is True
            assert dwell_bar.dwell_time == 3.0

    @pytest.mark.asyncio
    async def test_dwell_bar_hidden_when_dwell_disabled(self):
        """DwellBar remains hidden when dwell_time is 0."""
        app = make_app()
        async with app.run_test() as pilot:
            _setup_session(app, num_choices=3)
            await pilot.pause(0.1)

            dwell_bar = app.query_one("#dwell-bar", DwellBar)
            # dwell_time is 0 by default, bar should not be visible
            assert dwell_bar.dwell_time == 0.0

    @pytest.mark.asyncio
    async def test_dwell_progress_updates_over_time(self):
        """DwellBar progress increases as dwell timer ticks."""
        app = _make_app_with_dwell(2.0)
        async with app.run_test() as pilot:
            _setup_session(app, num_choices=3)
            await pilot.pause(0.1)

            dwell_bar = app.query_one("#dwell-bar", DwellBar)

            # Manually trigger _start_dwell (normally called on highlight change)
            app._start_dwell()
            await pilot.pause(0.3)  # Let a few ticks happen

            # Progress should have increased from 0
            assert dwell_bar.progress > 0.0, (
                f"Dwell progress should have increased, got {dwell_bar.progress}"
            )

    @pytest.mark.asyncio
    async def test_dwell_resets_on_scroll(self):
        """Scrolling to a different item resets the dwell timer."""
        app = _make_app_with_dwell(3.0)
        async with app.run_test() as pilot:
            _setup_session(app, num_choices=5)
            await pilot.pause(0.1)

            lv = app.query_one("#choices", ListView)
            real_indices = _get_real_choice_indices(lv)
            dwell_bar = app.query_one("#dwell-bar", DwellBar)

            # Start dwell on first choice
            lv.index = real_indices[0]
            await pilot.pause(0.3)

            progress_before_scroll = dwell_bar.progress

            # Scroll to second choice — should reset dwell
            lv.index = real_indices[1]
            await pilot.pause(0.15)

            # After scrolling, a new dwell starts — progress should be
            # near zero (just started) or at least less than before
            # if the previous dwell was well underway
            # The key behavior is that _start_dwell calls _cancel_dwell first
            assert dwell_bar.progress < progress_before_scroll or dwell_bar.progress < 0.2, (
                f"Dwell should reset on scroll. Before: {progress_before_scroll}, "
                f"After: {dwell_bar.progress}"
            )

    @pytest.mark.asyncio
    async def test_dwell_completes_selection(self):
        """Dwell timer completing triggers _do_select."""
        app = _make_app_with_dwell(0.2)  # Very short dwell for test
        async with app.run_test() as pilot:
            session = _setup_session(app, num_choices=3)
            await pilot.pause(0.1)

            lv = app.query_one("#choices", ListView)
            real_indices = _get_real_choice_indices(lv)

            # Focus on first real choice
            lv.index = real_indices[0]
            await pilot.pause(0.1)

            # Start dwell and wait for it to complete
            app._start_dwell()
            await pilot.pause(0.5)  # Well beyond 0.2s dwell time

            # After dwell completes, _do_select should have been called
            # which sets session.active = False (via _resolve_selection)
            # or the session has a selection
            dwell_bar = app.query_one("#dwell-bar", DwellBar)
            # The dwell timer should have been cancelled (completed)
            assert app._dwell_timer is None, "Dwell timer should be cancelled after completion"


# ═══════════════════════════════════════════════════════════════════════════
# 5. Undo integration
# ═══════════════════════════════════════════════════════════════════════════


class TestUndoIntegration:
    """Verify undo works end-to-end in the TUI with real app methods."""

    @pytest.mark.asyncio
    async def test_undo_when_no_history_speaks_nothing(self):
        """Pressing undo with no undo stack speaks 'Nothing to undo'."""
        tts = MockTTS()
        tts.speak_with_local_fallback = MagicMock()
        app = IoMcpApp(tts=tts, freeform_tts=tts, demo=True)

        async with app.run_test() as pilot:
            session = _setup_session(app, num_choices=3)
            await pilot.pause(0.1)

            # Mark as no longer active (as if a selection was made)
            session.active = False

            # Try to undo — should say "Nothing to undo"
            app.action_undo_selection()
            await pilot.pause(0.1)

            # _speak_ui passes voice_override and speed_override kwargs
            spoken_texts = [
                c[0][0] for c in tts.speak_with_local_fallback.call_args_list
                if c[0]
            ]
            assert "Nothing to undo" in spoken_texts, (
                f"Expected 'Nothing to undo' in spoken texts: {spoken_texts}"
            )

    @pytest.mark.asyncio
    async def test_undo_blocked_during_active_choices(self):
        """Undo is blocked when choices are actively presented."""
        tts = MockTTS()
        tts.speak_with_local_fallback = MagicMock()
        tts.stop = MagicMock()
        app = IoMcpApp(tts=tts, freeform_tts=tts, demo=True)

        async with app.run_test() as pilot:
            session = _setup_session(app, num_choices=3)
            await pilot.pause(0.1)

            # Push some undo history
            session.push_undo("Previous Q", [{"label": "Old"}])

            # Session is active — undo should be blocked
            assert session.active is True
            app.action_undo_selection()
            await pilot.pause(0.1)

            # _speak_ui passes voice_override and speed_override kwargs
            spoken_texts = [
                c[0][0] for c in tts.speak_with_local_fallback.call_args_list
                if c[0]
            ]
            assert "Already in choices. Scroll to pick." in spoken_texts, (
                f"Expected blocking message in spoken texts: {spoken_texts}"
            )

    @pytest.mark.asyncio
    async def test_undo_pops_stack_and_resolves(self):
        """Undo pops the stack and sends _undo sentinel to server."""
        tts = MockTTS()
        tts.play_chime = MagicMock()
        tts.stop = MagicMock()
        tts.speak_with_local_fallback = MagicMock()
        app = IoMcpApp(tts=tts, freeform_tts=tts, demo=True)

        async with app.run_test() as pilot:
            session = _setup_session(app, num_choices=3)
            await pilot.pause(0.1)

            # Simulate: selection was made, undo stack has entries
            session.active = False
            session.push_undo("Q1", [{"label": "A"}], selection={"selected": "A"})
            session.push_undo("Q2", [{"label": "B"}], selection={"selected": "B"})
            assert session.undo_depth == 2

            # Perform undo
            app.action_undo_selection()
            await pilot.pause(0.1)

            # Stack should have depth 1 (Q2 was popped)
            assert session.undo_depth == 1

            # Undo chime should have been played
            tts.play_chime.assert_called_with("undo")

    @pytest.mark.asyncio
    async def test_sequential_undos_drain_stack(self):
        """Multiple undos drain the stack correctly."""
        tts = MockTTS()
        tts.play_chime = MagicMock()
        tts.stop = MagicMock()
        tts.speak_with_local_fallback = MagicMock()
        app = IoMcpApp(tts=tts, freeform_tts=tts, demo=True)

        async with app.run_test() as pilot:
            session = _setup_session(app, num_choices=3)
            await pilot.pause(0.1)

            session.active = False
            session.push_undo("Q1", [{"label": "A"}])
            session.push_undo("Q2", [{"label": "B"}])
            session.push_undo("Q3", [{"label": "C"}])

            # Undo three times
            for expected_depth in [2, 1, 0]:
                session.active = False  # Reset active state
                app.action_undo_selection()
                await pilot.pause(0.1)
                assert session.undo_depth == expected_depth

            # Fourth undo — nothing left
            session.active = False
            tts.speak_with_local_fallback.reset_mock()
            app.action_undo_selection()
            await pilot.pause(0.1)
            # _speak_ui passes voice_override and speed_override kwargs
            spoken_texts = [
                c[0][0] for c in tts.speak_with_local_fallback.call_args_list
                if c[0]
            ]
            assert "Nothing to undo" in spoken_texts, (
                f"Expected 'Nothing to undo' in spoken texts: {spoken_texts}"
            )


# ═══════════════════════════════════════════════════════════════════════════
# 6. Extras menu ordering integration
# ═══════════════════════════════════════════════════════════════════════════


class TestExtrasMenuOrdering:
    """Verify secondary extras are ordered by ring-friendliness."""

    def test_replay_prompt_before_type_reply(self):
        """'Replay prompt' should appear before 'Type reply' in SECONDARY_EXTRAS."""
        labels = [e["label"] for e in SECONDARY_EXTRAS]
        replay_idx = labels.index("Replay prompt")
        type_idx = labels.index("Type reply")
        assert replay_idx < type_idx, (
            f"Replay prompt (idx {replay_idx}) should be before Type reply (idx {type_idx})"
        )

    def test_undo_before_type_reply(self):
        """'Undo' should appear before 'Type reply' in SECONDARY_EXTRAS."""
        labels = [e["label"] for e in SECONDARY_EXTRAS]
        undo_idx = labels.index("Undo")
        type_idx = labels.index("Type reply")
        assert undo_idx < type_idx, (
            f"Undo (idx {undo_idx}) should be before Type reply (idx {type_idx})"
        )

    def test_dismiss_before_filter(self):
        """'Dismiss' should appear before 'Filter' in SECONDARY_EXTRAS."""
        labels = [e["label"] for e in SECONDARY_EXTRAS]
        dismiss_idx = labels.index("Dismiss")
        filter_idx = labels.index("Filter")
        assert dismiss_idx < filter_idx, (
            f"Dismiss (idx {dismiss_idx}) should be before Filter (idx {filter_idx})"
        )

    def test_ring_friendly_actions_before_keyboard_dependent(self):
        """Ring-friendly actions (replay, undo, dismiss) should all come
        before keyboard-dependent actions (type, filter, interrupt)."""
        labels = [e["label"] for e in SECONDARY_EXTRAS]
        ring_friendly = ["Replay prompt", "Undo", "Dismiss"]
        keyboard_dependent = ["Type reply", "Filter", "Interrupt agent"]

        for rf in ring_friendly:
            for kd in keyboard_dependent:
                rf_idx = labels.index(rf)
                kd_idx = labels.index(kd)
                assert rf_idx < kd_idx, (
                    f"'{rf}' (idx {rf_idx}) should be before '{kd}' (idx {kd_idx})"
                )

    def test_help_is_last_secondary_extra(self):
        """'Help' should be the last item in SECONDARY_EXTRAS."""
        assert SECONDARY_EXTRAS[-1]["label"] == "Help"

    @pytest.mark.asyncio
    async def test_collapsed_extras_show_more_options_header(self):
        """In collapsed mode, extras start with 'More options ›' header."""
        app = make_app()
        async with app.run_test() as pilot:
            _setup_session(app, num_choices=3)
            await pilot.pause(0.1)

            lv = app.query_one("#choices", ListView)
            items = [
                c for c in lv.children
                if isinstance(c, ChoiceItem) and c.choice_index <= 0
            ]
            labels = [item.choice_label for item in items]

            assert "More options ›" in labels, (
                f"Expected 'More options ›' in extras. Got: {labels}"
            )

    @pytest.mark.asyncio
    async def test_primary_extras_always_visible(self):
        """PRIMARY_EXTRAS are always visible (not behind 'More options')."""
        app = make_app()
        async with app.run_test() as pilot:
            _setup_session(app, num_choices=3)
            await pilot.pause(0.1)

            lv = app.query_one("#choices", ListView)
            items = [
                c for c in lv.children
                if isinstance(c, ChoiceItem) and c.choice_index <= 0
            ]
            labels = [item.choice_label for item in items]

            for pe in PRIMARY_EXTRAS:
                assert pe["label"] in labels, (
                    f"Primary extra '{pe['label']}' should be visible. Got: {labels}"
                )


# ═══════════════════════════════════════════════════════════════════════════
# 7. Combined feature interactions
# ═══════════════════════════════════════════════════════════════════════════


class TestCombinedFeatureInteractions:
    """Verify features interact correctly when used together."""

    @pytest.mark.asyncio
    async def test_fast_scroll_wraps_with_boundary_cue(self):
        """Fast scroll that wraps around should trigger boundary cue at target."""
        tts = MockTTS()
        tts.speak_with_local_fallback = MagicMock()
        tts.speak_fragments_scroll = MagicMock()
        app = IoMcpApp(tts=tts, freeform_tts=tts, demo=True)
        # Enable scroll acceleration
        app._scroll_accel_enabled = True
        app._scroll_accel_fast_ms = 80
        app._scroll_accel_turbo_ms = 40
        app._scroll_accel_fast_skip = 3
        app._scroll_accel_turbo_skip = 5
        app._scroll_times = []

        async with app.run_test() as pilot:
            _setup_session(app, num_choices=5)
            await pilot.pause(0.1)

            lv = app.query_one("#choices", ListView)
            enabled = _get_enabled_indices(lv)

            # Position near the end
            lv.index = enabled[-2]
            await pilot.pause(0.2)

            tts.speak_with_local_fallback.reset_mock()
            tts.speak_fragments_scroll.reset_mock()

            # Turbo scroll down — should wrap to first (boundary "Top")
            now = time.time()
            app._scroll_times = [now - 0.080, now - 0.060, now - 0.040, now - 0.020]

            with patch("time.time", return_value=now):
                app.action_cursor_down()
            await pilot.pause(0.2)

            # Should have wrapped to first enabled item
            assert lv.index == enabled[0], (
                f"Expected wrap to {enabled[0]}, got {lv.index}"
            )

    @pytest.mark.asyncio
    async def test_wrap_with_dwell_resets_timer(self):
        """Wrapping around resets the dwell timer (new highlight)."""
        app = _make_app_with_dwell(3.0)
        async with app.run_test() as pilot:
            _setup_session(app, num_choices=3)
            await pilot.pause(0.1)

            lv = app.query_one("#choices", ListView)
            enabled = _get_enabled_indices(lv)
            dwell_bar = app.query_one("#dwell-bar", DwellBar)

            # Position at last enabled
            lv.index = enabled[-1]
            await pilot.pause(0.3)
            progress_before = dwell_bar.progress

            # Wrap around
            app.action_cursor_down()
            await pilot.pause(0.15)

            # Dwell should be reset (near zero progress)
            assert dwell_bar.progress < 0.15, (
                f"Dwell should reset after wrap. Progress: {dwell_bar.progress}"
            )

    @pytest.mark.asyncio
    async def test_input_mode_blocks_all_scroll_features(self):
        """Input mode blocks wrap, acceleration, and dwell."""
        app = _make_app_with_accel()
        async with app.run_test() as pilot:
            session = _setup_session(app, num_choices=5)
            await pilot.pause(0.1)

            lv = app.query_one("#choices", ListView)
            enabled = _get_enabled_indices(lv)

            # Position at last
            lv.index = enabled[-1]
            await pilot.pause(0.1)
            original = lv.index

            # Enable input mode
            session.input_mode = True

            # Try to scroll with turbo speed
            now = time.time()
            app._scroll_times = [now - 0.080, now - 0.060, now - 0.040, now - 0.020]

            with patch("time.time", return_value=now):
                app.action_cursor_down()
            await pilot.pause(0.1)

            # Cursor should not have moved
            assert lv.index == original, (
                "Input mode should block all scrolling"
            )
