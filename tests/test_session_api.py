"""Tests for session management, API events, and CLI tool."""

import threading
import time

import pytest

from io_mcp.session import Session, SessionManager, SpeechEntry, HistoryEntry


class TestSession:
    """Tests for Session dataclass."""

    def test_default_values(self):
        s = Session(session_id="test-1", name="Agent 1")
        assert s.session_id == "test-1"
        assert s.name == "Agent 1"
        assert s.active is False
        assert s.preamble == ""
        assert s.choices == []
        assert s.selection is None
        assert s.scroll_index == 0
        assert s.input_mode is False
        assert s.voice_recording is False
        assert s.ambient_count == 0
        assert s.pending_messages == []
        assert s.history == []
        assert s.speech_log == []

    def test_touch_updates_activity(self):
        s = Session(session_id="test-1", name="Agent 1")
        old_activity = s.last_activity
        time.sleep(0.01)
        s.touch()
        assert s.last_activity > old_activity

    def test_drain_messages_empty(self):
        s = Session(session_id="test-1", name="Agent 1")
        assert s.drain_messages() == ""

    def test_drain_messages_with_content(self):
        s = Session(session_id="test-1", name="Agent 1")
        s.pending_messages.append("hello")
        s.pending_messages.append("world")
        result = s.drain_messages()
        assert "hello" in result
        assert "world" in result
        assert s.pending_messages == []  # drained

    def test_selection_event(self):
        s = Session(session_id="test-1", name="Agent 1")
        assert not s.selection_event.is_set()
        s.selection_event.set()
        assert s.selection_event.is_set()
        s.selection_event.clear()
        assert not s.selection_event.is_set()

    def test_undo_support_fields(self):
        s = Session(session_id="test-1", name="Agent 1")
        assert s.last_preamble == ""
        assert s.last_choices == []
        s.last_preamble = "test preamble"
        s.last_choices = [{"label": "opt1"}]
        assert s.last_preamble == "test preamble"
        assert len(s.last_choices) == 1


class TestSpeechEntry:
    """Tests for SpeechEntry dataclass."""

    def test_default_values(self):
        e = SpeechEntry(text="hello")
        assert e.text == "hello"
        assert e.played is False
        assert e.priority == 0
        assert e.timestamp > 0

    def test_priority(self):
        e = SpeechEntry(text="urgent", priority=1)
        assert e.priority == 1


class TestHistoryEntry:
    """Tests for HistoryEntry dataclass."""

    def test_creation(self):
        h = HistoryEntry(label="Fix bug", summary="Fixed null check", preamble="Choose action")
        assert h.label == "Fix bug"
        assert h.summary == "Fixed null check"
        assert h.preamble == "Choose action"
        assert h.timestamp > 0


class TestSessionManagerExtended:
    """Extended tests for SessionManager beyond the existing ones."""

    def test_all_sessions_returns_ordered(self):
        m = SessionManager()
        s1, _ = m.get_or_create("a")
        s2, _ = m.get_or_create("b")
        s3, _ = m.get_or_create("c")
        all_sessions = m.all_sessions()
        assert [s.session_id for s in all_sessions] == ["a", "b", "c"]

    def test_count(self):
        m = SessionManager()
        assert m.count() == 0
        m.get_or_create("a")
        assert m.count() == 1
        m.get_or_create("b")
        assert m.count() == 2

    def test_next_with_choices_no_active(self):
        m = SessionManager()
        s1, _ = m.get_or_create("a")
        s2, _ = m.get_or_create("b")
        result = m.next_with_choices()
        assert result is None

    def test_next_with_choices_finds_active(self):
        m = SessionManager()
        s1, _ = m.get_or_create("a")
        s2, _ = m.get_or_create("b")
        s2.active = True
        result = m.next_with_choices()
        assert result is s2

    def test_tab_bar_text(self):
        m = SessionManager()
        assert m.tab_bar_text() == ""
        s1, _ = m.get_or_create("a")
        s1.name = "Agent 1"
        text = m.tab_bar_text()
        assert "Agent 1" in text

    def test_tab_bar_with_active_indicator(self):
        m = SessionManager()
        s1, _ = m.get_or_create("a")
        s1.name = "Agent 1"
        s1.active = True
        text = m.tab_bar_text()
        assert "●" in text

    def test_focus_nonexistent(self):
        m = SessionManager()
        result = m.focus("nonexistent")
        assert result is None

    def test_get_nonexistent(self):
        m = SessionManager()
        result = m.get("nonexistent")
        assert result is None

    def test_remove_nonexistent(self):
        m = SessionManager()
        m.get_or_create("a")
        result = m.remove("nonexistent")
        assert result == "a"  # active session unchanged


class TestFrontendEvents:
    """Tests for Frontend API event system."""

    def test_frontend_event_to_sse(self):
        from io_mcp.api import FrontendEvent
        event = FrontendEvent(
            event_type="test_event",
            data={"key": "value"},
            session_id="test-session",
        )
        sse = event.to_sse()
        assert "event: test_event" in sse
        assert "data:" in sse
        assert "value" in sse

    def test_event_bus_subscribe_unsubscribe(self):
        from io_mcp.api import EventBus
        bus = EventBus()
        q = bus.subscribe()
        assert q is not None
        bus.unsubscribe(q)

    def test_event_bus_publish(self):
        from io_mcp.api import EventBus, FrontendEvent
        bus = EventBus()
        q = bus.subscribe()
        event = FrontendEvent(event_type="test", data={"x": 1})
        bus.publish(event)
        received = q.get(timeout=1)
        assert received.event_type == "test"
        assert received.data["x"] == 1
        bus.unsubscribe(q)

    def test_emit_functions(self):
        """Test that emit helper functions don't crash."""
        from io_mcp.api import (
            emit_choices_presented,
            emit_speech_requested,
            emit_session_created,
            emit_session_removed,
            emit_selection_made,
            emit_recording_state,
        )
        # These should not raise even without subscribers
        emit_choices_presented("s1", "preamble", [{"label": "a"}])
        emit_speech_requested("s1", "hello", blocking=False)
        emit_session_created("s1", "Agent 1")
        emit_session_removed("s1")
        emit_selection_made("s1", "chosen", "summary")
        emit_recording_state("s1", True)
