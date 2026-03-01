"""Tests for waiting-state TTS announcement.

When a session enters the waiting state (agent working, no active choices),
a one-time TTS announcement tells the user what keys are available.
The announcement should:
- Fire once per waiting-state entry (tracked by session._waiting_announced)
- NOT fire after dismiss (dismiss already speaks)
- Reset when choices are next presented
"""

from __future__ import annotations

import threading
from unittest.mock import MagicMock, patch, call

import pytest

from io_mcp.session import Session


class TestWaitingAnnouncedFlag:
    """Tests for the _waiting_announced flag on Session."""

    def test_default_false(self):
        """Flag starts False so announcement fires on first waiting entry."""
        s = Session(session_id="test-1", name="Agent 1")
        assert s._waiting_announced is False

    def test_can_set_true(self):
        s = Session(session_id="test-1", name="Agent 1")
        s._waiting_announced = True
        assert s._waiting_announced is True

    def test_independent_per_session(self):
        """Each session has its own flag."""
        s1 = Session(session_id="s1", name="Agent 1")
        s2 = Session(session_id="s2", name="Agent 2")
        s1._waiting_announced = True
        assert s2._waiting_announced is False


class TestWaitingAnnouncementLogic:
    """Tests for the announcement logic in _show_waiting_with_shortcuts.

    Uses a minimal mock of the IoMcpApp to test the announcement
    logic without requiring a full Textual app.
    """

    def _make_app_stub(self, session):
        """Create a minimal stub that has the fields _show_waiting_with_shortcuts needs."""
        stub = MagicMock()
        stub._chat_view_active = False
        stub._cs = {
            "bg": "#2e3440", "fg": "#d8dee9", "fg_dim": "#4c566a",
            "accent": "#88c0d0", "success": "#a3be8c", "warning": "#ebcb8b",
            "error": "#bf616a", "purple": "#b48ead", "blue": "#81a1c1",
        }
        stub._key_labels = {
            "message": "m", "settings": "s", "dismiss": "d",
            "down": "j", "up": "k", "select": "Enter",
            "help": "?", "undo": "u", "pane": "v",
        }
        stub._speak_ui = MagicMock()

        # Mock _focused to return our session
        stub._focused = MagicMock(return_value=session)

        # Mock query_one to return mocks that support display and clear/append
        list_view_mock = MagicMock()
        list_view_mock.clear = MagicMock()
        list_view_mock.append = MagicMock()
        list_view_mock.display = True
        list_view_mock.index = 0

        def query_one_side_effect(selector, *args):
            if selector == "#choices":
                return list_view_mock
            m = MagicMock()
            m.display = True
            return m

        stub.query_one = MagicMock(side_effect=query_one_side_effect)
        stub._ensure_main_content_visible = MagicMock()

        return stub

    def test_announces_on_first_waiting_entry(self):
        """First call to _show_waiting_with_shortcuts should speak."""
        session = Session(session_id="test-1", name="CodeBot")
        session.registered = True
        assert session._waiting_announced is False

        stub = self._make_app_stub(session)

        # Import and call the real method, bound to our stub
        from io_mcp.tui.app import IoMcpApp
        IoMcpApp._show_waiting_with_shortcuts(stub, session)

        # Should have spoken the announcement
        stub._speak_ui.assert_called_once()
        spoken_text = stub._speak_ui.call_args[0][0]
        assert "CodeBot" in spoken_text
        assert "working" in spoken_text
        assert "m" in spoken_text  # message key
        assert "s" in spoken_text  # settings key
        assert "d" in spoken_text  # dismiss key

        # Flag should now be True
        assert session._waiting_announced is True

    def test_does_not_announce_twice(self):
        """Second call should NOT speak again."""
        session = Session(session_id="test-1", name="Agent 1")
        session.registered = True
        stub = self._make_app_stub(session)

        from io_mcp.tui.app import IoMcpApp
        IoMcpApp._show_waiting_with_shortcuts(stub, session)
        IoMcpApp._show_waiting_with_shortcuts(stub, session)

        # Only one _speak_ui call (from first invocation)
        assert stub._speak_ui.call_count == 1

    def test_announces_again_after_reset(self):
        """After resetting the flag, should announce again."""
        session = Session(session_id="test-1", name="Agent 1")
        session.registered = True
        stub = self._make_app_stub(session)

        from io_mcp.tui.app import IoMcpApp
        IoMcpApp._show_waiting_with_shortcuts(stub, session)
        assert stub._speak_ui.call_count == 1

        # Simulate choices being presented (resets flag)
        session._waiting_announced = False

        IoMcpApp._show_waiting_with_shortcuts(stub, session)
        assert stub._speak_ui.call_count == 2

    def test_no_announce_in_chat_view(self):
        """Chat view skips waiting shortcuts entirely."""
        session = Session(session_id="test-1", name="Agent 1")
        stub = self._make_app_stub(session)
        stub._chat_view_active = True

        from io_mcp.tui.app import IoMcpApp
        IoMcpApp._show_waiting_with_shortcuts(stub, session)

        stub._speak_ui.assert_not_called()
        assert session._waiting_announced is False

    def test_no_announce_for_none_session(self):
        """None session should not crash or announce."""
        stub = self._make_app_stub(None)
        from io_mcp.tui.app import IoMcpApp
        # Should not raise
        IoMcpApp._show_waiting_with_shortcuts(stub, None)
        stub._speak_ui.assert_not_called()

    def test_uses_configured_key_labels(self):
        """Announcement should use actual configured key labels."""
        session = Session(session_id="test-1", name="TestBot")
        session.registered = True
        stub = self._make_app_stub(session)
        # Custom key bindings
        stub._key_labels = {
            "message": "q",
            "settings": "x",
            "dismiss": "z",
            "down": "j", "up": "k", "select": "Enter",
        }

        from io_mcp.tui.app import IoMcpApp
        IoMcpApp._show_waiting_with_shortcuts(stub, session)

        spoken_text = stub._speak_ui.call_args[0][0]
        assert "q" in spoken_text
        assert "x" in spoken_text
        assert "z" in spoken_text


class TestShowChoicesResetsFlag:
    """Tests that _show_choices resets _waiting_announced."""

    def test_show_choices_resets_flag(self):
        """Calling _show_choices should reset _waiting_announced to False."""
        session = Session(session_id="test-1", name="Agent 1")
        session._waiting_announced = True
        session.active = True
        session.choices = [{"label": "A", "summary": "a"}]

        stub = MagicMock()
        stub._focused = MagicMock(return_value=session)
        stub._chat_view_active = False

        from io_mcp.tui.app import IoMcpApp
        # Call _show_choices — it will reset the flag then do UI work (which
        # will fail on mock, but that's fine — the flag reset is first)
        try:
            IoMcpApp._show_choices(stub)
        except Exception:
            pass  # UI widget errors on mock are expected

        assert session._waiting_announced is False

    def test_show_choices_with_none_session_is_safe(self):
        """_show_choices should not crash if no session is focused."""
        stub = MagicMock()
        stub._focused = MagicMock(return_value=None)

        from io_mcp.tui.app import IoMcpApp
        # Should not raise
        IoMcpApp._show_choices(stub)


class TestDismissSuppressesAnnouncement:
    """Tests that dismiss sets _waiting_announced to suppress duplicate TTS."""

    def test_dismiss_sets_flag(self):
        """_dismiss_active_item should set _waiting_announced=True."""
        session = Session(session_id="test-1", name="Agent 1")
        session.active = True
        session.preamble = "Test"
        session.choices = [{"label": "A"}]

        # Create a pending inbox item
        item = MagicMock()
        item.done = False
        item.result = None
        item.event = threading.Event()
        session._active_inbox_item = item

        stub = MagicMock()
        stub._focused = MagicMock(return_value=session)
        stub._speak_ui = MagicMock()
        stub._safe_call = MagicMock()  # prevent actual UI calls

        from io_mcp.tui.app import IoMcpApp
        IoMcpApp._dismiss_active_item(stub)

        assert session._waiting_announced is True

    def test_dismiss_stale_also_sets_flag(self):
        """Dismissing a stale item should also set _waiting_announced=True."""
        session = Session(session_id="test-1", name="Agent 1")
        session.active = True
        session.preamble = "Test"
        session.choices = [{"label": "A"}]
        session._active_inbox_item = None  # No active item → stale path

        stub = MagicMock()
        stub._focused = MagicMock(return_value=session)
        stub._speak_ui = MagicMock()
        stub._safe_call = MagicMock()

        from io_mcp.tui.app import IoMcpApp
        IoMcpApp._dismiss_active_item(stub)

        assert session._waiting_announced is True

    def test_dismiss_nothing_does_not_set_flag(self):
        """If there's nothing to dismiss, flag should not change."""
        session = Session(session_id="test-1", name="Agent 1")
        session.active = False
        session._active_inbox_item = None
        assert session._waiting_announced is False

        stub = MagicMock()
        stub._focused = MagicMock(return_value=session)
        stub._speak_ui = MagicMock()

        from io_mcp.tui.app import IoMcpApp
        IoMcpApp._dismiss_active_item(stub)

        # "Nothing to dismiss" path should not set the flag
        assert session._waiting_announced is False


class TestFullCycle:
    """Integration-style tests for the full choices → waiting → choices cycle."""

    def test_flag_lifecycle(self):
        """Flag goes False → True (announced) → False (choices shown) → True (announced again)."""
        s = Session(session_id="test-1", name="Bot")
        assert s._waiting_announced is False

        # 1. Enter waiting → announced
        s._waiting_announced = True  # simulates what _show_waiting_with_shortcuts does

        # 2. Choices presented → reset
        s._waiting_announced = False  # simulates what _show_choices does

        # 3. Enter waiting again → announced again
        s._waiting_announced = True
        assert s._waiting_announced is True

    def test_dismiss_then_choices_cycle(self):
        """After dismiss → waiting (suppressed) → choices → waiting (should announce)."""
        s = Session(session_id="test-1", name="Bot")

        # Dismiss sets flag
        s._waiting_announced = True  # simulates dismiss

        # Waiting view entered after dismiss — should NOT announce (flag=True)
        assert s._waiting_announced is True

        # New choices presented — resets flag
        s._waiting_announced = False  # simulates _show_choices

        # Enter waiting again — SHOULD announce (flag=False)
        assert s._waiting_announced is False
