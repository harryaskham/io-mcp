"""Tests for multi-level undo support.

Tests the undo stack on Session (push/pop/depth/cap) and the
TUI's action_undo_selection behavior with depth indicators.
"""

from unittest import mock

import pytest

from io_mcp.session import Session


# ── Session undo stack tests ──────────────────────────────────────


class TestUndoStack:
    """Tests for the per-session undo stack (push_undo / pop_undo / undo_depth)."""

    def test_push_single(self):
        """Pushing one entry increases depth to 1."""
        s = Session(session_id="t1", name="A")
        s.push_undo("Pick one", [{"label": "Opt A"}])
        assert s.undo_depth == 1

    def test_push_updates_legacy_fields(self):
        """push_undo keeps last_preamble / last_choices in sync."""
        s = Session(session_id="t2", name="A")
        s.push_undo("Q1", [{"label": "X"}])
        assert s.last_preamble == "Q1"
        assert s.last_choices == [{"label": "X"}]

    def test_pop_returns_entry(self):
        """pop_undo returns the most recent entry."""
        s = Session(session_id="t3", name="A")
        s.push_undo("Q1", [{"label": "A"}], selection={"selected": "A"})
        entry = s.pop_undo()
        assert entry is not None
        assert entry["preamble"] == "Q1"
        assert entry["choices"] == [{"label": "A"}]
        assert entry["selection"] == {"selected": "A"}

    def test_pop_decreases_depth(self):
        """pop_undo decreases undo_depth by 1."""
        s = Session(session_id="t4", name="A")
        s.push_undo("Q1", [{"label": "A"}])
        s.push_undo("Q2", [{"label": "B"}])
        assert s.undo_depth == 2
        s.pop_undo()
        assert s.undo_depth == 1

    def test_pop_empty_returns_none(self):
        """pop_undo on empty stack returns None."""
        s = Session(session_id="t5", name="A")
        assert s.pop_undo() is None

    def test_pop_updates_legacy_to_new_top(self):
        """After pop, legacy fields reflect the new top of stack."""
        s = Session(session_id="t6", name="A")
        s.push_undo("Q1", [{"label": "A"}])
        s.push_undo("Q2", [{"label": "B"}])
        s.pop_undo()  # removes Q2
        assert s.last_preamble == "Q1"
        assert s.last_choices == [{"label": "A"}]

    def test_pop_clears_legacy_when_empty(self):
        """After popping the last entry, legacy fields are cleared."""
        s = Session(session_id="t7", name="A")
        s.push_undo("Q1", [{"label": "A"}])
        s.pop_undo()
        assert s.last_preamble == ""
        assert s.last_choices == []

    def test_multiple_undos_in_sequence(self):
        """Undoing 3 selections in sequence works correctly."""
        s = Session(session_id="t8", name="A")
        s.push_undo("Q1", [{"label": "A"}], selection={"selected": "A"})
        s.push_undo("Q2", [{"label": "B"}], selection={"selected": "B"})
        s.push_undo("Q3", [{"label": "C"}], selection={"selected": "C"})
        assert s.undo_depth == 3

        # Undo #1: pops Q3
        e3 = s.pop_undo()
        assert e3["preamble"] == "Q3"
        assert s.undo_depth == 2

        # Undo #2: pops Q2
        e2 = s.pop_undo()
        assert e2["preamble"] == "Q2"
        assert s.undo_depth == 1

        # Undo #3: pops Q1
        e1 = s.pop_undo()
        assert e1["preamble"] == "Q1"
        assert s.undo_depth == 0

        # No more undos
        assert s.pop_undo() is None

    def test_stack_limited_to_max_depth(self):
        """Stack is capped at _undo_stack_max (default 5)."""
        s = Session(session_id="t9", name="A")
        assert s._undo_stack_max == 5

        for i in range(8):
            s.push_undo(f"Q{i}", [{"label": f"Opt{i}"}])

        assert s.undo_depth == 5
        # Oldest entries (Q0, Q1, Q2) should be dropped
        entry = s.undo_stack[0]
        assert entry["preamble"] == "Q3"  # Q0, Q1, Q2 trimmed

    def test_stack_is_per_session(self):
        """Each session has its own independent undo stack."""
        s1 = Session(session_id="s1", name="Agent 1")
        s2 = Session(session_id="s2", name="Agent 2")

        s1.push_undo("S1-Q1", [{"label": "A"}])
        s1.push_undo("S1-Q2", [{"label": "B"}])

        s2.push_undo("S2-Q1", [{"label": "X"}])

        assert s1.undo_depth == 2
        assert s2.undo_depth == 1

        # Popping from s1 doesn't affect s2
        s1.pop_undo()
        assert s1.undo_depth == 1
        assert s2.undo_depth == 1

    def test_push_preserves_choices_copy(self):
        """push_undo stores a copy of the choices list, not a reference."""
        s = Session(session_id="t10", name="A")
        choices = [{"label": "A"}]
        s.push_undo("Q", choices)

        # Mutating the original should not affect the stored copy
        choices.append({"label": "B"})
        assert len(s.undo_stack[0]["choices"]) == 1

    def test_push_with_selection(self):
        """push_undo stores the selection data."""
        s = Session(session_id="t11", name="A")
        s.push_undo("Q1", [{"label": "A"}], selection={"selected": "A", "summary": "do A"})
        assert s.undo_stack[0]["selection"] == {"selected": "A", "summary": "do A"}

    def test_push_without_selection(self):
        """push_undo defaults selection to None."""
        s = Session(session_id="t12", name="A")
        s.push_undo("Q1", [{"label": "A"}])
        assert s.undo_stack[0]["selection"] is None

    def test_undo_depth_property(self):
        """undo_depth reflects the current stack size."""
        s = Session(session_id="t13", name="A")
        assert s.undo_depth == 0
        s.push_undo("Q1", [])
        assert s.undo_depth == 1
        s.push_undo("Q2", [])
        assert s.undo_depth == 2
        s.pop_undo()
        assert s.undo_depth == 1
        s.pop_undo()
        assert s.undo_depth == 0


# ── TUI action_undo_selection tests ───────────────────────────────


class TestActionUndoSelection:
    """Tests for IoMcpApp.action_undo_selection with multi-level undo."""

    def _make_app_mock(self, session):
        """Create a mock IoMcpApp with the given session."""
        from io_mcp.tui.app import IoMcpApp

        app = mock.MagicMock(spec=IoMcpApp)
        app._focused = mock.MagicMock(return_value=session)
        app._in_settings = False
        app._chat_view_active = False
        app._tts = mock.MagicMock()
        app._vibrate = mock.MagicMock()
        app._resolve_selection = mock.MagicMock()
        app._speak_ui = mock.MagicMock()
        return app

    def test_empty_stack_speaks_nothing_to_undo(self):
        """When undo stack is empty, speak 'Nothing to undo'."""
        from io_mcp.tui.app import IoMcpApp

        session = Session(session_id="u1", name="Test")
        session.active = False
        # No push_undo — stack is empty
        app = self._make_app_mock(session)

        IoMcpApp.action_undo_selection(app)

        app._speak_ui.assert_called_once_with("Nothing to undo")
        # Should NOT call play_chime or _resolve_selection
        app._tts.play_chime.assert_not_called()
        app._resolve_selection.assert_not_called()

    def test_undo_speaks_remaining_count(self):
        """Undo should speak how many more undos are available."""
        from io_mcp.tui.app import IoMcpApp

        session = Session(session_id="u2", name="Test")
        session.active = False
        session.push_undo("Q1", [{"label": "A"}])
        session.push_undo("Q2", [{"label": "B"}])
        session.push_undo("Q3", [{"label": "C"}])

        app = self._make_app_mock(session)

        IoMcpApp.action_undo_selection(app)

        # After popping Q3, 2 remain (Q1, Q2)
        app._speak_ui.assert_called_once_with("Undo. 2 more available")

    def test_undo_last_entry_speaks_no_more(self):
        """When undoing the last entry, speak 'No more undos left'."""
        from io_mcp.tui.app import IoMcpApp

        session = Session(session_id="u3", name="Test")
        session.active = False
        session.push_undo("Q1", [{"label": "A"}])

        app = self._make_app_mock(session)

        IoMcpApp.action_undo_selection(app)

        app._speak_ui.assert_called_once_with("Undo. No more undos left")

    def test_undo_pops_from_stack(self):
        """action_undo_selection pops the top entry from the undo stack."""
        from io_mcp.tui.app import IoMcpApp

        session = Session(session_id="u4", name="Test")
        session.active = False
        session.push_undo("Q1", [{"label": "A"}])
        session.push_undo("Q2", [{"label": "B"}])

        app = self._make_app_mock(session)

        IoMcpApp.action_undo_selection(app)

        # Q2 was popped, only Q1 remains
        assert session.undo_depth == 1
        assert session.undo_stack[0]["preamble"] == "Q1"

    def test_undo_sends_undo_sentinel(self):
        """action_undo_selection resolves with _undo sentinel."""
        from io_mcp.tui.app import IoMcpApp

        session = Session(session_id="u5", name="Test")
        session.active = False
        session.push_undo("Q1", [{"label": "A"}])

        app = self._make_app_mock(session)

        IoMcpApp.action_undo_selection(app)

        app._resolve_selection.assert_called_once_with(
            session, {"selected": "_undo", "summary": ""}
        )

    def test_undo_plays_chime(self):
        """action_undo_selection should play the undo chime."""
        from io_mcp.tui.app import IoMcpApp

        session = Session(session_id="u6", name="Test")
        session.active = False
        session.push_undo("Q1", [{"label": "A"}])

        app = self._make_app_mock(session)

        IoMcpApp.action_undo_selection(app)

        app._tts.play_chime.assert_called_with("undo")

    def test_undo_vibrates(self):
        """action_undo_selection should vibrate on undo."""
        from io_mcp.tui.app import IoMcpApp

        session = Session(session_id="u7", name="Test")
        session.active = False
        session.push_undo("Q1", [{"label": "A"}])

        app = self._make_app_mock(session)

        IoMcpApp.action_undo_selection(app)

        app._vibrate.assert_called_with(100)

    def test_undo_stops_tts_before_chime(self):
        """TTS should be stopped before playing the undo chime."""
        from io_mcp.tui.app import IoMcpApp

        session = Session(session_id="u8", name="Test")
        session.active = False
        session.push_undo("Q1", [{"label": "A"}])

        app = self._make_app_mock(session)

        call_order = []
        app._tts.stop.side_effect = lambda: call_order.append("stop")
        app._tts.play_chime.side_effect = lambda name: call_order.append(f"chime:{name}")

        IoMcpApp.action_undo_selection(app)

        assert call_order.index("stop") < call_order.index("chime:undo")

    def test_multiple_undos_decrease_depth(self):
        """Multiple sequential undos decrease the stack depth correctly."""
        from io_mcp.tui.app import IoMcpApp

        session = Session(session_id="u9", name="Test")
        session.active = False
        session.push_undo("Q1", [{"label": "A"}])
        session.push_undo("Q2", [{"label": "B"}])
        session.push_undo("Q3", [{"label": "C"}])

        # Undo #1
        app = self._make_app_mock(session)
        IoMcpApp.action_undo_selection(app)
        assert session.undo_depth == 2

        # Undo #2 (must reset session.active since _resolve_selection was mocked)
        session.active = False
        app2 = self._make_app_mock(session)
        IoMcpApp.action_undo_selection(app2)
        assert session.undo_depth == 1

        # Undo #3
        session.active = False
        app3 = self._make_app_mock(session)
        IoMcpApp.action_undo_selection(app3)
        assert session.undo_depth == 0

        # Undo #4 — stack empty
        session.active = False
        app4 = self._make_app_mock(session)
        IoMcpApp.action_undo_selection(app4)
        app4._speak_ui.assert_called_once_with("Nothing to undo")
        app4._resolve_selection.assert_not_called()

    def test_undo_blocked_during_active_choices(self):
        """Undo should not work when choices are actively being presented."""
        from io_mcp.tui.app import IoMcpApp

        session = Session(session_id="u10", name="Test")
        session.active = True  # choices are active
        session.push_undo("Q1", [{"label": "A"}])

        app = self._make_app_mock(session)

        IoMcpApp.action_undo_selection(app)

        app._speak_ui.assert_called_once_with("Already in choices. Scroll to pick.")
        app._resolve_selection.assert_not_called()
        # Stack should be untouched
        assert session.undo_depth == 1

    def test_undo_blocked_in_input_mode(self):
        """Undo should not work when user is in input mode."""
        from io_mcp.tui.app import IoMcpApp

        session = Session(session_id="u11", name="Test")
        session.active = False
        session.input_mode = True
        session.push_undo("Q1", [{"label": "A"}])

        app = self._make_app_mock(session)

        IoMcpApp.action_undo_selection(app)

        # Should return early without speaking anything
        app._speak_ui.assert_not_called()
        app._resolve_selection.assert_not_called()

    def test_undo_blocked_in_settings(self):
        """Undo should not work when user is in settings menu."""
        from io_mcp.tui.app import IoMcpApp

        session = Session(session_id="u12", name="Test")
        session.active = False
        session.push_undo("Q1", [{"label": "A"}])

        app = self._make_app_mock(session)
        app._in_settings = True

        IoMcpApp.action_undo_selection(app)

        app._speak_ui.assert_not_called()
        app._resolve_selection.assert_not_called()
