"""Tests for stale/dead session cleanup.

Covers:
1. _auto_cleanup_dead_session resolves pending inbox items
2. _auto_cleanup_dead_session removes session from manager
3. Health monitor triggers cleanup for dead sessions
4. Quick settings includes "Clean stale sessions" option
"""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock, patch, call

import pytest

from io_mcp.session import (
    Session,
    SessionManager,
    InboxItem,
    _resolve_pending_inbox,
)


# ═══════════════════════════════════════════════════════════════════
# 1. _auto_cleanup_dead_session resolves pending items
# ═══════════════════════════════════════════════════════════════════


class TestAutoCleanupResolvesPendingItems:
    """_auto_cleanup_dead_session should resolve all pending inbox items."""

    def _make_app_stub(self):
        """Create a minimal mock app with required attributes."""
        stub = MagicMock()
        stub.manager = SessionManager()
        stub._speak_ui = MagicMock()
        stub.on_session_removed = MagicMock()
        return stub

    def test_resolves_pending_choices(self):
        """Pending choice items should be resolved with _cancelled."""
        stub = self._make_app_stub()
        session, _ = stub.manager.get_or_create("s1")
        session.name = "TestAgent"

        # Add a pending inbox item (simulating a present_choices call)
        item = InboxItem(
            kind="choices",
            preamble="Pick one",
            choices=[{"label": "A"}, {"label": "B"}],
        )
        session.enqueue(item)
        session.active = True

        from io_mcp.tui.app import IoMcpApp
        IoMcpApp._auto_cleanup_dead_session(stub, session)

        # Item should be resolved with _cancelled
        assert item.done is True
        assert item.result["selected"] == "_cancelled"
        assert item.event.is_set()

    def test_resolves_multiple_pending_items(self):
        """All pending items should be resolved, not just the first."""
        stub = self._make_app_stub()
        session, _ = stub.manager.get_or_create("s1")
        session.name = "TestAgent"

        items = []
        for i in range(3):
            item = InboxItem(
                kind="choices",
                preamble=f"Question {i}",
                choices=[{"label": "Yes"}, {"label": "No"}],
            )
            session.enqueue(item)
            items.append(item)

        from io_mcp.tui.app import IoMcpApp
        IoMcpApp._auto_cleanup_dead_session(stub, session)

        for item in items:
            assert item.done is True
            assert item.result["selected"] == "_cancelled"
            assert item.event.is_set()

    def test_unblocks_waiting_threads(self):
        """Threads blocked on item.event.wait() should be unblocked."""
        stub = self._make_app_stub()
        session, _ = stub.manager.get_or_create("s1")
        session.name = "TestAgent"

        item = InboxItem(
            kind="choices",
            preamble="Pick one",
            choices=[{"label": "A"}],
        )
        session.enqueue(item)

        unblocked = threading.Event()

        def wait_for_item():
            item.event.wait(timeout=5)
            unblocked.set()

        t = threading.Thread(target=wait_for_item, daemon=True)
        t.start()

        from io_mcp.tui.app import IoMcpApp
        IoMcpApp._auto_cleanup_dead_session(stub, session)

        assert unblocked.wait(timeout=2), "Waiting thread was not unblocked"

    def test_clears_active_state(self):
        """Session active state should be cleared."""
        stub = self._make_app_stub()
        session, _ = stub.manager.get_or_create("s1")
        session.name = "TestAgent"
        session.active = True
        session.preamble = "Something"
        session.choices = [{"label": "X"}]

        from io_mcp.tui.app import IoMcpApp
        IoMcpApp._auto_cleanup_dead_session(stub, session)

        assert session.active is False
        assert session.preamble == ""
        assert session.choices == []

    def test_speaks_cleanup_message(self):
        """Should speak a cleanup message with the session name."""
        stub = self._make_app_stub()
        session, _ = stub.manager.get_or_create("s1")
        session.name = "DeadBot"

        from io_mcp.tui.app import IoMcpApp
        IoMcpApp._auto_cleanup_dead_session(stub, session)

        stub._speak_ui.assert_called()
        spoken = stub._speak_ui.call_args[0][0]
        assert "DeadBot" in spoken
        assert "Cleaned up" in spoken


# ═══════════════════════════════════════════════════════════════════
# 2. _auto_cleanup_dead_session removes session from manager
# ═══════════════════════════════════════════════════════════════════


class TestAutoCleanupRemovesSession:
    """_auto_cleanup_dead_session should remove the session from the manager."""

    def _make_app_stub(self):
        stub = MagicMock()
        stub.manager = SessionManager()
        stub._speak_ui = MagicMock()
        # Wire on_session_removed to call manager.remove
        def real_remove(sid):
            stub.manager.remove(sid)
        stub.on_session_removed = MagicMock(side_effect=real_remove)
        return stub

    def test_session_removed_from_manager(self):
        """Session should no longer exist in the manager after cleanup."""
        stub = self._make_app_stub()
        session, _ = stub.manager.get_or_create("s1")
        session.name = "DeadAgent"

        assert stub.manager.get("s1") is not None

        from io_mcp.tui.app import IoMcpApp
        IoMcpApp._auto_cleanup_dead_session(stub, session)

        assert stub.manager.get("s1") is None

    def test_on_session_removed_called(self):
        """on_session_removed should be called with the session ID."""
        stub = self._make_app_stub()
        session, _ = stub.manager.get_or_create("s1")
        session.name = "DeadAgent"

        from io_mcp.tui.app import IoMcpApp
        IoMcpApp._auto_cleanup_dead_session(stub, session)

        stub.on_session_removed.assert_called_once_with("s1")

    def test_multiple_sessions_only_target_removed(self):
        """Only the target session should be removed; others preserved."""
        stub = self._make_app_stub()
        s1, _ = stub.manager.get_or_create("s1")
        s1.name = "LiveAgent"
        s2, _ = stub.manager.get_or_create("s2")
        s2.name = "DeadAgent"

        from io_mcp.tui.app import IoMcpApp
        IoMcpApp._auto_cleanup_dead_session(stub, s2)

        assert stub.manager.get("s1") is not None
        assert stub.manager.get("s2") is None

    def test_session_count_decrements(self):
        """Session count should decrease by one."""
        stub = self._make_app_stub()
        s1, _ = stub.manager.get_or_create("s1")
        s2, _ = stub.manager.get_or_create("s2")
        s1.name = "A"
        s2.name = "B"

        assert stub.manager.count() == 2

        from io_mcp.tui.app import IoMcpApp
        IoMcpApp._auto_cleanup_dead_session(stub, s2)

        assert stub.manager.count() == 1


# ═══════════════════════════════════════════════════════════════════
# 3. Health monitor triggers cleanup for dead sessions
# ═══════════════════════════════════════════════════════════════════


class TestHealthMonitorTriggersCleanup:
    """Health monitor should call _auto_cleanup_dead_session for dead sessions."""

    def _make_app_stub(self):
        stub = MagicMock()
        stub.manager = SessionManager()
        stub._config = MagicMock()
        stub._config.health_monitor_enabled = True
        stub._config.health_warning_threshold = 300.0
        stub._config.health_unresponsive_threshold = 600.0
        stub._config.health_check_tmux_pane = True
        stub._config.health_check_interval = 30
        stub._tts = MagicMock()
        stub._speak_ui = MagicMock()
        stub._notifier = MagicMock()
        stub._auto_cleanup_dead_session = MagicMock()
        stub._fire_health_alert = MagicMock()
        stub._vibrate_pattern = MagicMock()
        stub.call_from_thread = MagicMock()
        stub._update_tab_bar = MagicMock()
        stub.on_session_removed = MagicMock()
        return stub

    def test_dead_pane_and_old_triggers_cleanup(self):
        """Dead tmux pane + >5min elapsed should trigger auto cleanup."""
        stub = self._make_app_stub()
        session, _ = stub.manager.get_or_create("s1")
        session.name = "DeadAgent"
        session.tmux_pane = "%42"
        session.last_tool_call = time.time() - 400  # 6+ minutes ago
        session.health_status = "unresponsive"
        # Focus a different session so s1 is not focused
        s2, _ = stub.manager.get_or_create("s2")
        stub.manager.focus("s2")

        # Mock _is_tmux_pane_dead to return True for the dead session
        stub._is_tmux_pane_dead = MagicMock(side_effect=lambda s: s.session_id == "s1")

        from io_mcp.tui.app import IoMcpApp
        IoMcpApp._check_agent_health_inner(stub)

        stub._auto_cleanup_dead_session.assert_called()
        cleaned = [c[0][0].session_id for c in stub._auto_cleanup_dead_session.call_args_list]
        assert "s1" in cleaned

    def test_dead_pane_but_recent_does_not_trigger(self):
        """Dead tmux pane but recent activity (<5min) should NOT auto-cleanup."""
        stub = self._make_app_stub()
        session, _ = stub.manager.get_or_create("s1")
        session.name = "RecentAgent"
        session.tmux_pane = "%42"
        session.last_tool_call = time.time() - 60  # only 1 minute ago
        # Focus a different session
        s2, _ = stub.manager.get_or_create("s2")
        stub.manager.focus("s2")

        stub._is_tmux_pane_dead = MagicMock(return_value=True)

        from io_mcp.tui.app import IoMcpApp
        IoMcpApp._check_agent_health_inner(stub)

        # Should NOT have auto-cleaned — pane is dead but activity is recent
        if stub._auto_cleanup_dead_session.called:
            cleaned = [c[0][0].session_id for c in stub._auto_cleanup_dead_session.call_args_list]
            assert "s1" not in cleaned

    def test_focused_session_never_cleaned(self):
        """The focused session should never be auto-cleaned, even if dead."""
        stub = self._make_app_stub()
        session, _ = stub.manager.get_or_create("s1")
        session.name = "FocusedDead"
        session.tmux_pane = "%42"
        session.last_tool_call = time.time() - 1000
        # s1 is the focused session (only session)

        stub._is_tmux_pane_dead = MagicMock(return_value=True)

        from io_mcp.tui.app import IoMcpApp
        IoMcpApp._check_agent_health_inner(stub)

        stub._auto_cleanup_dead_session.assert_not_called()

    def test_healthy_session_not_cleaned(self):
        """A healthy session with a live tmux pane should not be cleaned."""
        stub = self._make_app_stub()
        session, _ = stub.manager.get_or_create("s1")
        session.name = "HealthyAgent"
        session.tmux_pane = "%42"
        session.last_tool_call = time.time() - 10  # very recent
        s2, _ = stub.manager.get_or_create("s2")
        stub.manager.focus("s2")

        stub._is_tmux_pane_dead = MagicMock(return_value=False)

        from io_mcp.tui.app import IoMcpApp
        IoMcpApp._check_agent_health_inner(stub)

        stub._auto_cleanup_dead_session.assert_not_called()

    def test_unresponsive_no_tmux_triggers_cleanup(self):
        """Unresponsive session without tmux info should trigger cleanup."""
        stub = self._make_app_stub()
        session, _ = stub.manager.get_or_create("s1")
        session.name = "NoTmuxAgent"
        session.tmux_pane = ""  # no tmux info
        session.last_tool_call = time.time() - 700  # >10min
        session.health_status = "unresponsive"
        s2, _ = stub.manager.get_or_create("s2")
        stub.manager.focus("s2")

        stub._is_tmux_pane_dead = MagicMock(return_value=False)

        from io_mcp.tui.app import IoMcpApp
        IoMcpApp._check_agent_health_inner(stub)

        stub._auto_cleanup_dead_session.assert_called()
        cleaned = [c[0][0].session_id for c in stub._auto_cleanup_dead_session.call_args_list]
        assert "s1" in cleaned


# ═══════════════════════════════════════════════════════════════════
# 4. Quick settings includes "Clean stale sessions" option
# ═══════════════════════════════════════════════════════════════════


class TestQuickSettingsCleanOption:
    """Quick settings menu should include the 'Clean stale sessions' option."""

    def _make_app_stub(self):
        stub = MagicMock()
        stub.manager = SessionManager()
        stub._speak_ui = MagicMock()
        stub._tts = MagicMock()
        stub._cs = {"purple": "#b48ead"}
        stub.settings = MagicMock()
        stub.settings.speed = 1.2
        stub.settings.voice = "sage"
        stub._config = MagicMock()
        stub._config.djent_enabled = False
        stub._in_settings = False
        stub._setting_edit_mode = False
        stub._quick_settings_mode = False
        stub.on_session_removed = MagicMock()
        stub._auto_cleanup_dead_session = MagicMock()
        stub._enter_quick_settings = MagicMock()
        stub._show_dialog = MagicMock()
        return stub

    def test_clean_option_in_quick_settings_items(self):
        """_enter_quick_settings should include 'Clean stale sessions' item."""
        # We test indirectly by checking the handler routing
        stub = self._make_app_stub()

        from io_mcp.tui.app import IoMcpApp

        # Call _handle_quick_settings_select with "Clean stale sessions"
        # and verify it dispatches to _clean_stale_sessions_action
        stub._clean_stale_sessions_action = MagicMock()
        IoMcpApp._handle_quick_settings_select(stub, "Clean stale sessions")

        stub._clean_stale_sessions_action.assert_called_once()

    def test_clean_no_stale_sessions(self):
        """When no stale sessions exist, should speak 'No stale sessions'."""
        stub = self._make_app_stub()
        # All sessions are healthy
        s1, _ = stub.manager.get_or_create("s1")
        s1.health_status = "healthy"

        from io_mcp.tui.app import IoMcpApp
        IoMcpApp._clean_stale_sessions_action(stub)

        stub._speak_ui.assert_called()
        spoken = stub._speak_ui.call_args[0][0]
        assert "No stale" in spoken

    def test_clean_single_stale_auto_cleans(self):
        """Single stale session should be auto-cleaned without dialog."""
        stub = self._make_app_stub()
        s1, _ = stub.manager.get_or_create("s1")
        s1.name = "StaleBot"
        s1.health_status = "warning"

        from io_mcp.tui.app import IoMcpApp
        IoMcpApp._clean_stale_sessions_action(stub)

        stub._auto_cleanup_dead_session.assert_called_once_with(s1)
        stub._show_dialog.assert_not_called()

    def test_clean_multiple_stale_shows_dialog(self):
        """Multiple stale sessions should show a dialog for selection."""
        stub = self._make_app_stub()
        s1, _ = stub.manager.get_or_create("s1")
        s1.name = "StaleBot1"
        s1.health_status = "warning"
        s2, _ = stub.manager.get_or_create("s2")
        s2.name = "StaleBot2"
        s2.health_status = "unresponsive"

        from io_mcp.tui.app import IoMcpApp
        IoMcpApp._clean_stale_sessions_action(stub)

        stub._show_dialog.assert_called_once()
        dialog_call = stub._show_dialog.call_args
        title = dialog_call[1].get("title", dialog_call[0][0] if dialog_call[0] else "")
        assert "Stale" in title or "Clean" in title
        buttons = dialog_call[1].get("buttons", dialog_call[0][2] if len(dialog_call[0]) > 2 else [])
        labels = [b["label"] for b in buttons]
        assert "StaleBot1" in labels
        assert "StaleBot2" in labels
        assert "Clean all" in labels
        assert "Cancel" in labels

    def test_clean_healthy_sessions_excluded(self):
        """Healthy sessions should not appear in the stale list."""
        stub = self._make_app_stub()
        s1, _ = stub.manager.get_or_create("s1")
        s1.name = "HealthyBot"
        s1.health_status = "healthy"
        s2, _ = stub.manager.get_or_create("s2")
        s2.name = "StaleBot"
        s2.health_status = "unresponsive"

        from io_mcp.tui.app import IoMcpApp
        IoMcpApp._clean_stale_sessions_action(stub)

        # Only one stale session → auto-clean (no dialog)
        stub._auto_cleanup_dead_session.assert_called_once_with(s2)
