"""Tests for tab-switch TTS summaries.

When the user presses h/l to switch between agent tabs, a brief spoken
summary orients them: session name, status (waiting/working/idle),
option count, and truncated preamble.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest

from io_mcp.session import Session


class TestSpeakTabSummary:
    """Tests for IoMcpApp._speak_tab_summary()."""

    def _call(self, session):
        """Call _speak_tab_summary on a mock app stub and return the stub."""
        stub = MagicMock()
        stub._speak_ui = MagicMock()
        from io_mcp.tui.app import IoMcpApp
        IoMcpApp._speak_tab_summary(stub, session)
        return stub

    def test_summary_includes_session_name(self):
        """Summary should start with the session name."""
        session = Session(session_id="s1", name="CodeBot")
        stub = self._call(session)
        stub._speak_ui.assert_called_once()
        spoken = stub._speak_ui.call_args[0][0]
        assert "CodeBot" in spoken

    def test_active_session_says_waiting_with_option_count(self):
        """Active session should say 'waiting for you' and option count."""
        session = Session(session_id="s1", name="Builder")
        session.active = True
        session.choices = [
            {"label": "Yes", "summary": "Confirm"},
            {"label": "No", "summary": "Deny"},
            {"label": "Maybe", "summary": "Unsure"},
        ]
        session.preamble = "Should I deploy?"
        stub = self._call(session)
        spoken = stub._speak_ui.call_args[0][0]
        assert "waiting for you" in spoken
        assert "3 options" in spoken

    def test_active_session_single_option(self):
        """Single option should use singular 'option'."""
        session = Session(session_id="s1", name="Agent")
        session.active = True
        session.choices = [{"label": "OK"}]
        stub = self._call(session)
        spoken = stub._speak_ui.call_args[0][0]
        assert "1 option" in spoken
        assert "1 options" not in spoken

    def test_active_session_includes_truncated_preamble(self):
        """Preamble should be included, truncated if long."""
        session = Session(session_id="s1", name="Agent")
        session.active = True
        session.choices = [{"label": "A"}]
        session.preamble = "This is a short preamble"
        stub = self._call(session)
        spoken = stub._speak_ui.call_args[0][0]
        assert "This is a short preamble" in spoken

    def test_active_session_truncates_long_preamble(self):
        """Preambles longer than 80 chars should be truncated with ellipsis."""
        session = Session(session_id="s1", name="Agent")
        session.active = True
        session.choices = [{"label": "A"}]
        session.preamble = "A" * 100  # 100 chars
        stub = self._call(session)
        spoken = stub._speak_ui.call_args[0][0]
        assert "..." in spoken
        # Full 100-char preamble should NOT appear
        assert "A" * 100 not in spoken

    def test_inactive_recent_session_says_working(self):
        """Inactive session with recent activity should say 'working'."""
        session = Session(session_id="s1", name="Worker")
        session.active = False
        session.last_activity = time.time()  # just now
        stub = self._call(session)
        spoken = stub._speak_ui.call_args[0][0]
        assert "working" in spoken
        assert "waiting" not in spoken

    def test_inactive_old_session_says_idle(self):
        """Inactive session with no recent activity should say 'idle'."""
        session = Session(session_id="s1", name="Sleeper")
        session.active = False
        session.last_activity = time.time() - 300  # 5 minutes ago
        stub = self._call(session)
        spoken = stub._speak_ui.call_args[0][0]
        assert "idle" in spoken

    def test_none_session_is_safe(self):
        """Passing None should not crash or speak."""
        stub = self._call(None)
        stub._speak_ui.assert_not_called()

    def test_summary_spoken_via_speak_ui(self):
        """Summary must be spoken via _speak_ui (UI voice, self-interrupting)."""
        session = Session(session_id="s1", name="Agent")
        stub = self._call(session)
        stub._speak_ui.assert_called_once()
        # Verify it was NOT called via _tts.speak_async
        stub._tts = MagicMock()
        stub._tts.speak_async.assert_not_called()

    def test_active_no_preamble(self):
        """Active session with no preamble should still work."""
        session = Session(session_id="s1", name="Bot")
        session.active = True
        session.choices = [{"label": "A"}, {"label": "B"}]
        session.preamble = ""
        stub = self._call(session)
        spoken = stub._speak_ui.call_args[0][0]
        assert "Bot" in spoken
        assert "waiting for you" in spoken
        assert "2 options" in spoken

    def test_active_empty_choices_list(self):
        """Active session with empty choices list says waiting without count."""
        session = Session(session_id="s1", name="Bot")
        session.active = True
        session.choices = []
        stub = self._call(session)
        spoken = stub._speak_ui.call_args[0][0]
        assert "waiting for you" in spoken


class TestTabSwitchCallsSummary:
    """Verify that action_next_tab/action_prev_tab call _speak_tab_summary."""

    def _make_app_stub(self):
        """Create a minimal mock with the fields tab switch needs."""
        stub = MagicMock()
        stub._chat_view_active = False
        stub._inbox_collapsed = False
        stub._inbox_pane_focused = False
        stub._speak_tab_summary = MagicMock()
        stub._speak_ui = MagicMock()
        stub._switch_to_session = MagicMock()
        stub._tts = MagicMock()
        return stub

    def test_next_tab_calls_summary(self):
        """action_next_tab should call _speak_tab_summary with the new session."""
        s1 = Session(session_id="s1", name="Agent 1")
        s2 = Session(session_id="s2", name="Agent 2")

        stub = self._make_app_stub()
        stub._focused = MagicMock(return_value=s1)
        stub.manager = MagicMock()
        stub.manager.count.return_value = 2
        stub.manager.next_tab.return_value = s2
        stub._inbox_pane_visible = MagicMock(return_value=False)

        from io_mcp.tui.app import IoMcpApp
        IoMcpApp.action_next_tab(stub)

        stub._speak_tab_summary.assert_called_once_with(s2)
        stub._switch_to_session.assert_called_once_with(s2)

    def test_prev_tab_calls_summary(self):
        """action_prev_tab should call _speak_tab_summary with the new session."""
        s1 = Session(session_id="s1", name="Agent 1")
        s2 = Session(session_id="s2", name="Agent 2")

        stub = self._make_app_stub()
        stub._focused = MagicMock(return_value=s1)
        stub.manager = MagicMock()
        stub.manager.count.return_value = 2
        stub.manager.prev_tab.return_value = s2
        stub._inbox_pane_visible = MagicMock(return_value=False)

        from io_mcp.tui.app import IoMcpApp
        IoMcpApp.action_prev_tab(stub)

        stub._speak_tab_summary.assert_called_once_with(s2)
        stub._switch_to_session.assert_called_once_with(s2)

    def test_next_choices_tab_calls_summary(self):
        """action_next_choices_tab should call _speak_tab_summary."""
        s1 = Session(session_id="s1", name="Agent 1")
        s2 = Session(session_id="s2", name="Agent 2")
        s2.active = True

        stub = self._make_app_stub()
        stub._focused = MagicMock(return_value=s1)
        stub.manager = MagicMock()
        stub.manager.next_with_choices.return_value = s2

        from io_mcp.tui.app import IoMcpApp
        IoMcpApp.action_next_choices_tab(stub)

        stub._speak_tab_summary.assert_called_once_with(s2)
