"""Tests for scroll acceleration in io-mcp TUI.

When the user spins the smart ring fast, the cursor should skip items
to navigate long lists quickly. The acceleration is based on the average
interval between recent scroll events:

- Normal speed (>80ms avg interval): move 1 item
- Fast speed (40-80ms avg interval): skip 3 items (configurable)
- Turbo speed (<40ms avg interval): skip 5 items (configurable)

Covers:
1. Normal speed scrolling moves 1 item at a time
2. Fast scrolling moves fastSkip items at a time
3. Turbo scrolling moves turboSkip items at a time
4. Disabled acceleration always moves 1 item
5. Skip count is capped at list boundary (wraps around)
6. Config values are read correctly
"""

from __future__ import annotations

import time
from unittest.mock import patch

import pytest
from textual.widgets import ListView

from io_mcp.tui.app import IoMcpApp
from io_mcp.tui.widgets import ChoiceItem, EXTRA_OPTIONS
from io_mcp.session import Session
from io_mcp.config import IoMcpConfig

from tests.test_tui_pilot import MockTTS, make_app


# ─── Helpers ──────────────────────────────────────────────────────────


def _make_app_with_accel(**overrides) -> IoMcpApp:
    """Create a testable IoMcpApp with scroll acceleration config."""
    config = IoMcpConfig.load.__wrapped__(IoMcpConfig) if hasattr(IoMcpConfig.load, '__wrapped__') else None
    # Build a minimal config with scroll acceleration settings
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
    # Override scroll acceleration settings
    app._scroll_accel_enabled = cfg_dict["enabled"]
    app._scroll_accel_fast_ms = cfg_dict["fastThresholdMs"]
    app._scroll_accel_turbo_ms = cfg_dict["turboThresholdMs"]
    app._scroll_accel_fast_skip = cfg_dict["fastSkip"]
    app._scroll_accel_turbo_skip = cfg_dict["turboSkip"]
    app._scroll_times = []
    return app


def _setup_session(app, num_choices=10):
    """Create a session with many choices for scroll acceleration testing."""
    session, _ = app.manager.get_or_create("test-1")
    session.registered = True
    session.name = "Test"
    app.on_session_created(session)
    app._chat_view_active = False

    choices = [
        {"label": f"Item {i}", "summary": f"Option number {i}"}
        for i in range(num_choices)
    ]
    session.preamble = "Pick one"
    session.choices = choices
    session.active = True
    session.extras_count = len(EXTRA_OPTIONS)
    session.all_items = list(EXTRA_OPTIONS) + choices
    app._show_choices()
    return session


def _simulate_scrolls_at_interval(app, interval_sec: float, count: int):
    """Pre-fill the scroll time buffer to simulate scrolling at a given interval.

    Uses deterministic timestamps rather than real time.
    """
    base = time.time() - (count * interval_sec)
    app._scroll_times = [base + i * interval_sec for i in range(count)]


# ─── Tests: _scroll_skip_count ──────────────────────────────────────


class TestScrollSkipCount:
    """Unit tests for the _scroll_skip_count method."""

    def test_normal_speed_returns_1(self):
        """With >80ms intervals, skip count should be 1."""
        app = _make_app_with_accel()
        # Simulate 5 scrolls at 150ms intervals (normal speed)
        _simulate_scrolls_at_interval(app, 0.150, 5)
        # Next call adds current time to buffer
        result = app._scroll_skip_count()
        assert result == 1

    def test_fast_speed_returns_fast_skip(self):
        """With 40-80ms intervals, skip count should be fastSkip (3)."""
        app = _make_app_with_accel()
        # Simulate scrolls at 60ms intervals (fast)
        now = time.time()
        app._scroll_times = [now - 0.240, now - 0.180, now - 0.120, now - 0.060]
        with patch("time.time", return_value=now):
            result = app._scroll_skip_count()
        assert result == 3

    def test_turbo_speed_returns_turbo_skip(self):
        """With <40ms intervals, skip count should be turboSkip (5)."""
        app = _make_app_with_accel()
        # Simulate scrolls at 20ms intervals (turbo)
        now = time.time()
        app._scroll_times = [now - 0.080, now - 0.060, now - 0.040, now - 0.020]
        with patch("time.time", return_value=now):
            result = app._scroll_skip_count()
        assert result == 5

    def test_disabled_always_returns_1(self):
        """With acceleration disabled, always return 1."""
        app = _make_app_with_accel(enabled=False)
        # Even with fast intervals, should return 1
        now = time.time()
        app._scroll_times = [now - 0.080, now - 0.060, now - 0.040, now - 0.020]
        with patch("time.time", return_value=now):
            result = app._scroll_skip_count()
        assert result == 1

    def test_insufficient_samples_returns_1(self):
        """With fewer than 3 timestamps, return 1 (not enough data)."""
        app = _make_app_with_accel()
        # Only 1 existing timestamp
        app._scroll_times = [time.time() - 0.020]
        result = app._scroll_skip_count()
        assert result == 1

    def test_ring_buffer_capped_at_5(self):
        """Buffer never grows beyond 5 entries."""
        app = _make_app_with_accel()
        # Add many entries
        for _ in range(20):
            app._scroll_skip_count()
        assert len(app._scroll_times) <= 5

    def test_custom_thresholds(self):
        """Custom threshold values are respected."""
        app = _make_app_with_accel(
            fastThresholdMs=200,
            turboThresholdMs=100,
            fastSkip=4,
            turboSkip=8,
        )
        # 150ms intervals should be "fast" with 200ms threshold
        now = time.time()
        app._scroll_times = [now - 0.600, now - 0.450, now - 0.300, now - 0.150]
        with patch("time.time", return_value=now):
            result = app._scroll_skip_count()
        assert result == 4  # fastSkip

    def test_turbo_custom_thresholds(self):
        """Custom turbo threshold values are respected."""
        app = _make_app_with_accel(
            fastThresholdMs=200,
            turboThresholdMs=100,
            fastSkip=4,
            turboSkip=8,
        )
        # 50ms intervals should be "turbo" with 100ms threshold
        now = time.time()
        app._scroll_times = [now - 0.200, now - 0.150, now - 0.100, now - 0.050]
        with patch("time.time", return_value=now):
            result = app._scroll_skip_count()
        assert result == 8  # turboSkip


# ─── Tests: Full integration with action_cursor_down/up ─────────────


@pytest.mark.asyncio
async def test_normal_scroll_down_moves_1():
    """Normal speed scroll down moves exactly 1 enabled item."""
    app = _make_app_with_accel()
    async with app.run_test() as pilot:
        _setup_session(app, num_choices=10)
        app._show_choices()
        await pilot.pause(0.1)

        lv = app.query_one("#choices", ListView)
        enabled = [i for i, c in enumerate(list(lv.children)) if not c.disabled]
        assert len(enabled) >= 5

        # Start at first enabled item
        lv.index = enabled[0]
        await pilot.pause(0.1)

        # Clear scroll buffer and simulate slow scrolling
        app._scroll_times = []
        _simulate_scrolls_at_interval(app, 0.200, 4)  # 200ms intervals = slow

        app.action_cursor_down()
        await pilot.pause(0.1)
        assert lv.index == enabled[1], f"Expected {enabled[1]}, got {lv.index}"


@pytest.mark.asyncio
async def test_fast_scroll_down_skips_items():
    """Fast scroll down skips fastSkip (3) items."""
    app = _make_app_with_accel()
    async with app.run_test() as pilot:
        _setup_session(app, num_choices=10)
        app._show_choices()
        await pilot.pause(0.1)

        lv = app.query_one("#choices", ListView)
        enabled = [i for i, c in enumerate(list(lv.children)) if not c.disabled]
        assert len(enabled) >= 5

        # Start at first enabled item
        lv.index = enabled[0]
        await pilot.pause(0.1)

        # Simulate fast scrolling (60ms intervals)
        now = time.time()
        app._scroll_times = [now - 0.240, now - 0.180, now - 0.120, now - 0.060]

        with patch("time.time", return_value=now):
            app.action_cursor_down()
        await pilot.pause(0.1)
        assert lv.index == enabled[3], f"Expected skip to {enabled[3]}, got {lv.index}"


@pytest.mark.asyncio
async def test_turbo_scroll_down_skips_more():
    """Turbo scroll down skips turboSkip (5) items."""
    app = _make_app_with_accel()
    async with app.run_test() as pilot:
        _setup_session(app, num_choices=10)
        app._show_choices()
        await pilot.pause(0.1)

        lv = app.query_one("#choices", ListView)
        enabled = [i for i, c in enumerate(list(lv.children)) if not c.disabled]
        assert len(enabled) >= 6

        # Start at first enabled item
        lv.index = enabled[0]
        await pilot.pause(0.1)

        # Simulate turbo scrolling (20ms intervals)
        now = time.time()
        app._scroll_times = [now - 0.080, now - 0.060, now - 0.040, now - 0.020]

        with patch("time.time", return_value=now):
            app.action_cursor_down()
        await pilot.pause(0.1)
        assert lv.index == enabled[5], f"Expected turbo skip to {enabled[5]}, got {lv.index}"


@pytest.mark.asyncio
async def test_fast_scroll_up_skips_items():
    """Fast scroll up skips fastSkip (3) items backwards."""
    app = _make_app_with_accel()
    async with app.run_test() as pilot:
        _setup_session(app, num_choices=10)
        app._show_choices()
        await pilot.pause(0.1)

        lv = app.query_one("#choices", ListView)
        enabled = [i for i, c in enumerate(list(lv.children)) if not c.disabled]
        assert len(enabled) >= 5

        # Start at enabled[4] (5th enabled item)
        lv.index = enabled[4]
        await pilot.pause(0.1)

        # Simulate fast scrolling (60ms intervals)
        now = time.time()
        app._scroll_times = [now - 0.240, now - 0.180, now - 0.120, now - 0.060]

        with patch("time.time", return_value=now):
            app.action_cursor_up()
        await pilot.pause(0.1)
        assert lv.index == enabled[1], f"Expected skip back to {enabled[1]}, got {lv.index}"


@pytest.mark.asyncio
async def test_skip_wraps_at_end():
    """Skip count that goes past the end wraps to first item."""
    app = _make_app_with_accel()
    async with app.run_test() as pilot:
        _setup_session(app, num_choices=10)
        app._show_choices()
        await pilot.pause(0.1)

        lv = app.query_one("#choices", ListView)
        enabled = [i for i, c in enumerate(list(lv.children)) if not c.disabled]

        # Start near the end (2nd to last)
        lv.index = enabled[-2]
        await pilot.pause(0.1)

        # Simulate turbo scrolling (skip 5, but only 1 item left before end)
        now = time.time()
        app._scroll_times = [now - 0.080, now - 0.060, now - 0.040, now - 0.020]

        with patch("time.time", return_value=now):
            app.action_cursor_down()
        await pilot.pause(0.1)
        # Should wrap to first enabled
        assert lv.index == enabled[0], f"Expected wrap to {enabled[0]}, got {lv.index}"


@pytest.mark.asyncio
async def test_skip_wraps_at_beginning():
    """Skip count that goes past the beginning wraps to last item."""
    app = _make_app_with_accel()
    async with app.run_test() as pilot:
        _setup_session(app, num_choices=10)
        app._show_choices()
        await pilot.pause(0.1)

        lv = app.query_one("#choices", ListView)
        enabled = [i for i, c in enumerate(list(lv.children)) if not c.disabled]

        # Start near the beginning (2nd enabled item)
        lv.index = enabled[1]
        await pilot.pause(0.1)

        # Simulate turbo scrolling up (skip 5, but only 1 item before start)
        now = time.time()
        app._scroll_times = [now - 0.080, now - 0.060, now - 0.040, now - 0.020]

        with patch("time.time", return_value=now):
            app.action_cursor_up()
        await pilot.pause(0.1)
        # Should wrap to last enabled
        assert lv.index == enabled[-1], f"Expected wrap to {enabled[-1]}, got {lv.index}"


@pytest.mark.asyncio
async def test_disabled_accel_always_moves_1():
    """With acceleration disabled, scroll always moves 1 item regardless of speed."""
    app = _make_app_with_accel(enabled=False)
    async with app.run_test() as pilot:
        _setup_session(app, num_choices=10)
        app._show_choices()
        await pilot.pause(0.1)

        lv = app.query_one("#choices", ListView)
        enabled = [i for i, c in enumerate(list(lv.children)) if not c.disabled]
        assert len(enabled) >= 5

        # Start at first enabled item
        lv.index = enabled[0]
        await pilot.pause(0.1)

        # Simulate turbo-speed scrolling
        now = time.time()
        app._scroll_times = [now - 0.080, now - 0.060, now - 0.040, now - 0.020]

        with patch("time.time", return_value=now):
            app.action_cursor_down()
        await pilot.pause(0.1)
        # Should still only move 1
        assert lv.index == enabled[1], f"Expected single step to {enabled[1]}, got {lv.index}"


# ─── Tests: Config integration ──────────────────────────────────────


def test_config_scroll_acceleration_defaults():
    """Default config should have scroll acceleration enabled."""
    from io_mcp.config import DEFAULT_CONFIG
    sa = DEFAULT_CONFIG["config"]["scrollAcceleration"]
    assert sa["enabled"] is True
    assert sa["fastThresholdMs"] == 80
    assert sa["turboThresholdMs"] == 40
    assert sa["fastSkip"] == 3
    assert sa["turboSkip"] == 5


def test_config_property_returns_defaults():
    """IoMcpConfig.scroll_acceleration property returns correct defaults."""
    cfg = IoMcpConfig(raw={}, expanded={})
    sa = cfg.scroll_acceleration
    assert sa["enabled"] is True
    assert sa["fastThresholdMs"] == 80
    assert sa["turboThresholdMs"] == 40
    assert sa["fastSkip"] == 3
    assert sa["turboSkip"] == 5


def test_config_property_reads_custom_values():
    """IoMcpConfig.scroll_acceleration property reads custom values."""
    expanded = {
        "config": {
            "scrollAcceleration": {
                "enabled": False,
                "fastThresholdMs": 100,
                "turboThresholdMs": 50,
                "fastSkip": 4,
                "turboSkip": 8,
            }
        }
    }
    cfg = IoMcpConfig(raw={}, expanded=expanded)
    sa = cfg.scroll_acceleration
    assert sa["enabled"] is False
    assert sa["fastThresholdMs"] == 100
    assert sa["turboThresholdMs"] == 50
    assert sa["fastSkip"] == 4
    assert sa["turboSkip"] == 8


def test_config_validation_accepts_scroll_acceleration():
    """scrollAcceleration should not trigger unknown config key warning."""
    cfg = IoMcpConfig(
        raw={"config": {"scrollAcceleration": {"enabled": True}}},
        expanded={"config": {"scrollAcceleration": {"enabled": True}}},
    )
    cfg._validate()
    # No warning about scrollAcceleration being unknown
    for w in cfg.validation_warnings:
        assert "scrollAcceleration" not in w, f"Unexpected warning: {w}"
