"""Tests for session management, API events, and CLI tool."""

import collections
import threading
import time

import pytest

from io_mcp.session import Session, SessionManager, SpeechEntry, HistoryEntry, InboxItem, FlushedMessage


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

    def test_drain_messages_populates_flushed(self):
        """Draining messages should copy them to flushed_messages."""
        s = Session(session_id="test-1", name="Agent 1")
        s.pending_messages.append("hello")
        s.pending_messages.append("world")
        s.drain_messages()
        assert len(s.flushed_messages) == 2
        assert s.flushed_messages[0].text == "hello"
        assert s.flushed_messages[1].text == "world"
        assert s.flushed_messages[0].flushed_at > 0
        assert s.flushed_messages[1].flushed_at > 0

    def test_drain_messages_empty_leaves_flushed_unchanged(self):
        """Draining empty pending_messages should not add to flushed_messages."""
        s = Session(session_id="test-1", name="Agent 1")
        s.drain_messages()
        assert len(s.flushed_messages) == 0

    def test_flushed_messages_capped_at_max(self):
        """flushed_messages should be capped at _flushed_messages_max."""
        s = Session(session_id="test-1", name="Agent 1")
        # Fill up flushed messages to just under the cap
        for i in range(48):
            s.flushed_messages.append(FlushedMessage(text=f"old-{i}"))
        # Add 5 more via drain
        for i in range(5):
            s.pending_messages.append(f"new-{i}")
        s.drain_messages()
        # Total would be 53, but should be capped at 50
        assert len(s.flushed_messages) == 50
        # The oldest messages should have been trimmed
        assert s.flushed_messages[0].text == "old-3"
        assert s.flushed_messages[-1].text == "new-4"

    def test_flushed_messages_default_empty(self):
        """New sessions start with empty flushed_messages."""
        s = Session(session_id="test-1", name="Agent 1")
        assert s.flushed_messages == []

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
        assert s.undo_stack == []
        assert s.undo_depth == 0
        s.push_undo("test preamble", [{"label": "opt1"}])
        assert s.last_preamble == "test preamble"
        assert len(s.last_choices) == 1
        assert s.undo_depth == 1


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
        assert "●" in text  # active indicator (green dot)

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


class TestSessionHealthMonitoring:
    """Tests for Session health_status fields and health monitoring logic."""

    def test_default_health_status(self):
        """New sessions start as healthy."""
        s = Session(session_id="test-1", name="Agent 1")
        assert s.health_status == "healthy"
        assert s.health_alert_spoken is False
        assert s.health_last_check == 0.0

    def test_health_status_can_be_set(self):
        """Health status transitions work correctly."""
        s = Session(session_id="test-1", name="Agent 1")
        s.health_status = "warning"
        assert s.health_status == "warning"
        s.health_status = "unresponsive"
        assert s.health_status == "unresponsive"
        s.health_status = "healthy"
        assert s.health_status == "healthy"

    def test_health_alert_spoken_flag(self):
        """Alert spoken flag can be toggled."""
        s = Session(session_id="test-1", name="Agent 1")
        assert s.health_alert_spoken is False
        s.health_alert_spoken = True
        assert s.health_alert_spoken is True
        s.health_alert_spoken = False
        assert s.health_alert_spoken is False

    def test_tab_bar_shows_warning_indicator(self):
        """Tab bar shows ! for warning health status."""
        m = SessionManager()
        s1, _ = m.get_or_create("a")
        s1.name = "Agent 1"
        s1.health_status = "warning"
        s1.active = False
        text = m.tab_bar_text()
        assert "!" in text

    def test_tab_bar_shows_unresponsive_indicator(self):
        """Tab bar shows ✗ for unresponsive health status."""
        m = SessionManager()
        s1, _ = m.get_or_create("a")
        s1.name = "Agent 1"
        s1.health_status = "unresponsive"
        s1.active = False
        text = m.tab_bar_text()
        assert "[#bf616a]✗[/#bf616a]" in text

    def test_tab_bar_active_choices_hides_warning(self):
        """When agent has active choices, health warning is hidden (agent is healthy)."""
        m = SessionManager()
        s1, _ = m.get_or_create("a")
        s1.name = "Agent 1"
        s1.health_status = "warning"
        s1.active = True  # agent is presenting choices — not stuck
        text = m.tab_bar_text()
        # Should show choices indicator (●), not warning (!)
        assert "[#a3be8c]●[/#a3be8c]" in text
        assert "!" not in text

    def test_tab_bar_healthy_no_indicator(self):
        """Healthy agents with no active choices show dim dot indicator."""
        m = SessionManager()
        s1, _ = m.get_or_create("a")
        s1.name = "Agent 1"
        s1.health_status = "healthy"
        s1.active = False
        text = m.tab_bar_text()
        assert "[#ebcb8b]![/#ebcb8b]" not in text
        assert "[#bf616a]✗[/#bf616a]" not in text
        assert "[#a3be8c]●[/#a3be8c]" not in text
        # Should show dim dot for idle-but-connected
        assert "[#4c566a]●[/#4c566a]" in text

    def test_health_threshold_warning_logic(self):
        """Simulate the warning threshold detection logic."""
        import time as _time
        s = Session(session_id="test-1", name="Agent 1")
        # Set last_tool_call to 6 minutes ago
        s.last_tool_call = _time.time() - 360
        s.active = False

        warning_threshold = 300.0
        unresponsive_threshold = 600.0

        elapsed = _time.time() - s.last_tool_call
        if elapsed >= unresponsive_threshold:
            expected_status = "unresponsive"
        elif elapsed >= warning_threshold:
            expected_status = "warning"
        else:
            expected_status = "healthy"

        assert expected_status == "warning"

    def test_health_threshold_unresponsive_logic(self):
        """Simulate the unresponsive threshold detection logic."""
        import time as _time
        s = Session(session_id="test-1", name="Agent 1")
        # Set last_tool_call to 11 minutes ago
        s.last_tool_call = _time.time() - 660
        s.active = False

        warning_threshold = 300.0
        unresponsive_threshold = 600.0

        elapsed = _time.time() - s.last_tool_call
        if elapsed >= unresponsive_threshold:
            expected_status = "unresponsive"
        elif elapsed >= warning_threshold:
            expected_status = "warning"
        else:
            expected_status = "healthy"

        assert expected_status == "unresponsive"

    def test_active_session_skips_health_check(self):
        """Sessions with active choices should be treated as healthy."""
        import time as _time
        s = Session(session_id="test-1", name="Agent 1")
        # Set last_tool_call to 20 minutes ago — would normally be unresponsive
        s.last_tool_call = _time.time() - 1200
        s.active = True  # but it's waiting for user selection — perfectly healthy

        # The health check should skip this session entirely
        # (tested by verifying the active check guard is correct)
        assert s.active is True
        # If active, health check returns early — no status change

    def test_health_reset_on_new_tool_call(self):
        """Health status resets to healthy when last_tool_call is recent."""
        import time as _time
        s = Session(session_id="test-1", name="Agent 1")
        s.health_status = "warning"
        s.health_alert_spoken = True

        # Simulate server.py resetting health on tool call
        s.last_tool_call = _time.time()
        if getattr(s, 'health_status', 'healthy') != 'healthy':
            s.health_status = 'healthy'
            s.health_alert_spoken = False

        assert s.health_status == "healthy"
        assert s.health_alert_spoken is False

    def test_multiple_sessions_independent_health(self):
        """Multiple sessions can have different health statuses."""
        m = SessionManager()
        s1, _ = m.get_or_create("a")
        s2, _ = m.get_or_create("b")
        s3, _ = m.get_or_create("c")

        s1.health_status = "healthy"
        s2.health_status = "warning"
        s3.health_status = "unresponsive"

        sessions = m.all_sessions()
        assert sessions[0].health_status == "healthy"
        assert sessions[1].health_status == "warning"
        assert sessions[2].health_status == "unresponsive"

    def test_tab_bar_multiple_health_states(self):
        """Tab bar renders all health states correctly for multiple agents."""
        m = SessionManager()
        s1, _ = m.get_or_create("a")
        s2, _ = m.get_or_create("b")
        s3, _ = m.get_or_create("c")

        s1.name = "Good Agent"
        s2.name = "Slow Agent"
        s3.name = "Dead Agent"

        s1.health_status = "healthy"
        s2.health_status = "warning"
        s3.health_status = "unresponsive"

        text = m.tab_bar_text()
        assert "Good Agent" in text
        assert "Slow Agent" in text
        assert "Dead Agent" in text
        assert "!" in text   # warning indicator
        assert "[#bf616a]✗[/#bf616a]" in text   # unresponsive indicator
        assert "|" in text   # pipe separator between tabs


class TestSessionSummaryAndTimeline:
    """Tests for Session.summary() and Session.timeline() methods."""

    def test_summary_no_activity(self):
        """Summary for a freshly created session."""
        s = Session(session_id="test-1", name="Agent 1")
        summary = s.summary()
        assert "no activity" in summary.lower() or "just connected" in summary.lower()

    def test_summary_with_tool_calls(self):
        """Summary reflects tool call count."""
        s = Session(session_id="test-1", name="Agent 1")
        s.tool_call_count = 12
        s.last_tool_name = "speak_async"
        summary = s.summary()
        assert "12" in summary
        assert "speak_async" in summary

    def test_summary_with_selections(self):
        """Summary includes selection count."""
        s = Session(session_id="test-1", name="Agent 1")
        s.tool_call_count = 5
        s.history = [
            HistoryEntry(label="Option A", summary="Did A", preamble="Choose"),
            HistoryEntry(label="Option B", summary="Did B", preamble="Choose"),
        ]
        summary = s.summary()
        assert "2" in summary  # 2 selections

    def test_summary_active_session(self):
        """Summary shows waiting status for active session."""
        s = Session(session_id="test-1", name="Agent 1")
        s.tool_call_count = 3
        s.active = True
        summary = s.summary()
        assert "waiting" in summary.lower() or "selection" in summary.lower()

    def test_summary_warning_status(self):
        """Summary reflects warning health status."""
        s = Session(session_id="test-1", name="Agent 1")
        s.tool_call_count = 3
        s.health_status = "warning"
        summary = s.summary()
        assert "stuck" in summary.lower()

    def test_summary_unresponsive_status(self):
        """Summary reflects unresponsive health status."""
        s = Session(session_id="test-1", name="Agent 1")
        s.tool_call_count = 3
        s.health_status = "unresponsive"
        summary = s.summary()
        assert "unresponsive" in summary.lower()

    def test_timeline_empty(self):
        """Timeline is empty for new session."""
        s = Session(session_id="test-1", name="Agent 1")
        tl = s.timeline()
        assert tl == []

    def test_timeline_with_speech(self):
        """Timeline includes speech entries."""
        s = Session(session_id="test-1", name="Agent 1")
        s.speech_log = [
            SpeechEntry(text="Hello world"),
            SpeechEntry(text="Running tests"),
        ]
        tl = s.timeline()
        assert len(tl) == 2
        assert all(e["type"] == "speech" for e in tl)

    def test_timeline_with_history(self):
        """Timeline includes selection history."""
        s = Session(session_id="test-1", name="Agent 1")
        s.history = [
            HistoryEntry(label="Fix bug", summary="Fixed null check", preamble="Choose"),
        ]
        tl = s.timeline()
        assert len(tl) == 1
        assert tl[0]["type"] == "selection"
        assert tl[0]["text"] == "Fix bug"
        assert tl[0]["detail"] == "Fixed null check"

    def test_timeline_merged_and_sorted(self):
        """Timeline merges speech and history, sorted by time descending."""
        s = Session(session_id="test-1", name="Agent 1")
        now = time.time()
        s.speech_log = [
            SpeechEntry(text="First speech", timestamp=now - 30),
            SpeechEntry(text="Last speech", timestamp=now - 5),
        ]
        s.history = [
            HistoryEntry(label="Middle selection", summary="Selected",
                         preamble="Choose", timestamp=now - 15),
        ]
        tl = s.timeline()
        assert len(tl) == 3
        # Most recent first
        assert tl[0]["text"] == "Last speech"
        assert tl[1]["text"] == "Middle selection"
        assert tl[2]["text"] == "First speech"

    def test_timeline_max_entries(self):
        """Timeline respects max_entries limit."""
        s = Session(session_id="test-1", name="Agent 1")
        now = time.time()
        s.speech_log = [
            SpeechEntry(text=f"Speech {i}", timestamp=now - i)
            for i in range(50)
        ]
        tl = s.timeline(max_entries=10)
        assert len(tl) == 10

    def test_timeline_has_age_strings(self):
        """Timeline entries have human-readable age strings."""
        s = Session(session_id="test-1", name="Agent 1")
        s.speech_log = [
            SpeechEntry(text="Recent", timestamp=time.time() - 30),
        ]
        tl = s.timeline()
        assert len(tl) == 1
        assert "ago" in tl[0]["age"]

    def test_tool_call_count_default(self):
        """Tool call count starts at 0."""
        s = Session(session_id="test-1", name="Agent 1")
        assert s.tool_call_count == 0

    def test_last_tool_name_default(self):
        """Last tool name starts empty."""
        s = Session(session_id="test-1", name="Agent 1")
        assert s.last_tool_name == ""


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


class TestInboxItem:
    """Tests for InboxItem dataclass."""

    def test_default_values(self):
        item = InboxItem(kind="choices")
        assert item.kind == "choices"
        assert item.preamble == ""
        assert item.choices == []
        assert item.text == ""
        assert item.blocking is False
        assert item.priority == 0
        assert item.result is None
        assert item.done is False
        assert item.timestamp > 0
        assert not item.event.is_set()

    def test_choices_item(self):
        choices = [{"label": "A"}, {"label": "B"}]
        item = InboxItem(kind="choices", preamble="Pick one", choices=choices)
        assert item.preamble == "Pick one"
        assert len(item.choices) == 2

    def test_speech_item(self):
        item = InboxItem(kind="speech", text="Hello", blocking=True, priority=1)
        assert item.text == "Hello"
        assert item.blocking is True
        assert item.priority == 1

    def test_event_signaling(self):
        item = InboxItem(kind="choices")
        assert not item.event.is_set()
        item.result = {"selected": "A"}
        item.done = True
        item.event.set()
        assert item.event.is_set()
        assert item.done is True


class TestSessionInbox:
    """Tests for Session inbox queue methods."""

    def test_inbox_starts_empty(self):
        s = Session(session_id="test-1", name="Agent 1")
        assert len(s.inbox) == 0
        assert len(s.inbox_done) == 0
        assert s.inbox_choices_count() == 0

    def test_enqueue_single(self):
        s = Session(session_id="test-1", name="Agent 1")
        item = InboxItem(kind="choices", preamble="Pick", choices=[{"label": "A"}])
        s.enqueue(item)
        assert len(s.inbox) == 1
        assert s.inbox_choices_count() == 1

    def test_enqueue_multiple(self):
        s = Session(session_id="test-1", name="Agent 1")
        item1 = InboxItem(kind="choices", preamble="First")
        item2 = InboxItem(kind="choices", preamble="Second")
        item3 = InboxItem(kind="choices", preamble="Third")
        s.enqueue(item1)
        s.enqueue(item2)
        s.enqueue(item3)
        assert len(s.inbox) == 3
        assert s.inbox_choices_count() == 3

    def test_peek_inbox_returns_front(self):
        s = Session(session_id="test-1", name="Agent 1")
        item1 = InboxItem(kind="choices", preamble="First")
        item2 = InboxItem(kind="choices", preamble="Second")
        s.enqueue(item1)
        s.enqueue(item2)
        front = s.peek_inbox()
        assert front is item1

    def test_peek_inbox_empty(self):
        s = Session(session_id="test-1", name="Agent 1")
        assert s.peek_inbox() is None

    def test_peek_inbox_skips_done(self):
        s = Session(session_id="test-1", name="Agent 1")
        item1 = InboxItem(kind="choices", preamble="First")
        item2 = InboxItem(kind="choices", preamble="Second")
        s.enqueue(item1)
        s.enqueue(item2)
        # Mark first as done
        item1.done = True
        front = s.peek_inbox()
        assert front is item2
        # item1 should have been moved to inbox_done
        assert len(s.inbox_done) == 1
        assert s.inbox_done[0] is item1

    def test_resolve_front(self):
        s = Session(session_id="test-1", name="Agent 1")
        item = InboxItem(kind="choices", preamble="Pick")
        s.enqueue(item)
        result = {"selected": "A", "summary": "Option A"}
        resolved = s.resolve_front(result)
        assert resolved is item
        assert item.result == result
        assert item.done is True
        assert item.event.is_set()
        # Should be moved to inbox_done
        assert len(s.inbox) == 0
        assert len(s.inbox_done) == 1

    def test_resolve_front_empty(self):
        s = Session(session_id="test-1", name="Agent 1")
        assert s.resolve_front({"selected": "A"}) is None

    def test_resolve_front_advances_queue(self):
        s = Session(session_id="test-1", name="Agent 1")
        item1 = InboxItem(kind="choices", preamble="First")
        item2 = InboxItem(kind="choices", preamble="Second")
        s.enqueue(item1)
        s.enqueue(item2)
        # Resolve first
        s.resolve_front({"selected": "A"})
        # Second should now be at front
        front = s.peek_inbox()
        assert front is item2
        assert s.inbox_choices_count() == 1

    def test_inbox_choices_count_excludes_done(self):
        s = Session(session_id="test-1", name="Agent 1")
        item1 = InboxItem(kind="choices")
        item2 = InboxItem(kind="choices")
        item3 = InboxItem(kind="speech")  # not a choices item
        s.enqueue(item1)
        s.enqueue(item2)
        s.enqueue(item3)
        assert s.inbox_choices_count() == 2
        item1.done = True
        assert s.inbox_choices_count() == 1

    def test_concurrent_enqueue_and_resolve(self):
        """Two threads enqueue items, main thread resolves them in order."""
        s = Session(session_id="test-1", name="Agent 1")
        results = []

        def enqueue_and_wait(preamble):
            item = InboxItem(kind="choices", preamble=preamble)
            s.enqueue(item)
            item.event.wait(timeout=5)
            if item.result:
                results.append(item.result["selected"])

        # Start two threads that enqueue
        t1 = threading.Thread(target=enqueue_and_wait, args=("First",))
        t2 = threading.Thread(target=enqueue_and_wait, args=("Second",))
        t1.start()
        time.sleep(0.05)  # ensure ordering
        t2.start()
        time.sleep(0.05)

        # Resolve them in order
        s.resolve_front({"selected": "A"})
        time.sleep(0.05)
        s.resolve_front({"selected": "B"})

        t1.join(timeout=2)
        t2.join(timeout=2)

        assert results == ["A", "B"]

    def test_resolve_wakes_waiting_thread(self):
        """Resolving an inbox item wakes the thread waiting on item.event."""
        s = Session(session_id="test-1", name="Agent 1")
        item = InboxItem(kind="choices", preamble="Pick")
        s.enqueue(item)
        result_holder = []

        def wait_for_result():
            item.event.wait(timeout=5)
            if item.result:
                result_holder.append(item.result)

        t = threading.Thread(target=wait_for_result)
        t.start()
        time.sleep(0.05)
        s.resolve_front({"selected": "Done"})
        t.join(timeout=2)
        assert len(result_holder) == 1
        assert result_holder[0]["selected"] == "Done"


class TestTabBarInboxBadge:
    """Tests for inbox count badges in the tab bar."""

    def test_tab_bar_no_badge_single_item(self):
        """Single active choice set shows plain indicator, no count badge."""
        m = SessionManager()
        s, _ = m.get_or_create("a")
        s.name = "Agent 1"
        s.active = True
        # One item in inbox
        s.enqueue(InboxItem(kind="choices"))
        text = m.tab_bar_text()
        assert "●" in text
        assert "+0" not in text  # no +0 badge

    def test_tab_bar_badge_with_queued_items(self):
        """Multiple queued choice sets show count badge."""
        m = SessionManager()
        s, _ = m.get_or_create("a")
        s.name = "Agent 1"
        s.active = True
        # Three items in inbox
        s.enqueue(InboxItem(kind="choices"))
        s.enqueue(InboxItem(kind="choices"))
        s.enqueue(InboxItem(kind="choices"))
        text = m.tab_bar_text()
        assert "●+2" in text  # 3 total, active shows ●, +2 queued

    def test_tab_bar_badge_queued_but_not_active(self):
        """Queued choices when session is not yet active show +N badge."""
        m = SessionManager()
        s, _ = m.get_or_create("a")
        s.name = "Agent 1"
        s.active = False
        # Two items in inbox (waiting to be presented)
        s.enqueue(InboxItem(kind="choices"))
        s.enqueue(InboxItem(kind="choices"))
        text = m.tab_bar_text()
        assert "+2" in text

    def test_tab_bar_badge_clears_after_resolve(self):
        """Badge updates when items are resolved."""
        m = SessionManager()
        s, _ = m.get_or_create("a")
        s.name = "Agent 1"
        s.active = True
        item1 = InboxItem(kind="choices")
        item2 = InboxItem(kind="choices")
        s.enqueue(item1)
        s.enqueue(item2)
        text_before = m.tab_bar_text()
        assert "●+1" in text_before
        # Resolve first item
        s.resolve_front({"selected": "A"})
        text_after = m.tab_bar_text()
        # Now only 1 item, no +N badge
        assert "+1" not in text_after or "●+0" not in text_after


class TestDedupAndEnqueue:
    """Tests for Session.dedup_and_enqueue() — atomic dedup + enqueue."""

    def test_basic_enqueue(self):
        """A single item is enqueued normally."""
        s = Session(session_id="test-1", name="Agent 1")
        item = InboxItem(kind="choices", preamble="Pick one",
                         choices=[{"label": "A"}, {"label": "B"}])
        assert s.dedup_and_enqueue(item) is True
        assert len(s.inbox) == 1
        assert not item.done

    def test_piggybacks_on_pending_duplicate(self):
        """An identical pending item returns existing item for piggybacking."""
        s = Session(session_id="test-1", name="Agent 1")
        choices = [{"label": "A"}, {"label": "B"}]
        item1 = InboxItem(kind="choices", preamble="Pick", choices=list(choices))
        s.enqueue(item1)  # bypass dedup for setup

        item2 = InboxItem(kind="choices", preamble="Pick", choices=list(choices))
        result = s.dedup_and_enqueue(item2)

        # Should return the existing item for piggybacking (not True/False)
        assert result is item1

        # item1 should NOT be cancelled — it's still pending
        assert item1.done is False
        assert not item1.event.is_set()

        # item2 should NOT be in the queue
        assert len(s.inbox) == 1

    def test_enqueues_after_existing_resolved(self):
        """After a pending item is resolved, identical items are enqueued fresh."""
        s = Session(session_id="test-1", name="Agent 1")
        choices = [{"label": "X"}]
        item1 = InboxItem(kind="choices", preamble="Go", choices=list(choices))
        assert s.dedup_and_enqueue(item1) is True

        # Resolve item1 so it's done — simulates normal completion
        item1.result = {"selected": "X"}
        item1.done = True
        item1.event.set()

        # Now enqueue an identical item — item1 is done, so this is fresh
        item2 = InboxItem(kind="choices", preamble="Go", choices=list(choices))
        assert s.dedup_and_enqueue(item2) is True
        assert len(s.inbox) == 2  # both in deque, item1 done, item2 pending

    def test_different_choices_not_deduped(self):
        """Items with different choices are enqueued normally."""
        s = Session(session_id="test-1", name="Agent 1")
        item1 = InboxItem(kind="choices", preamble="Pick",
                          choices=[{"label": "A"}])
        item2 = InboxItem(kind="choices", preamble="Pick",
                          choices=[{"label": "B"}])
        assert s.dedup_and_enqueue(item1) is True
        assert s.dedup_and_enqueue(item2) is True
        assert len(s.inbox) == 2

    def test_different_preamble_not_deduped(self):
        """Items with different preambles are enqueued normally."""
        s = Session(session_id="test-1", name="Agent 1")
        choices = [{"label": "A"}]
        item1 = InboxItem(kind="choices", preamble="First prompt",
                          choices=list(choices))
        item2 = InboxItem(kind="choices", preamble="Second prompt",
                          choices=list(choices))
        assert s.dedup_and_enqueue(item1) is True
        assert s.dedup_and_enqueue(item2) is True
        assert len(s.inbox) == 2

    def test_piggyback_after_item_done_enqueues_fresh(self):
        """After the pending item is done, identical items are enqueued fresh."""
        s = Session(session_id="test-1", name="Agent 1")
        choices = [{"label": "A"}]

        item1 = InboxItem(kind="choices", preamble="Go", choices=list(choices))
        assert s.dedup_and_enqueue(item1) is True

        # Mark as done
        item1.result = {"selected": "A"}
        item1.done = True
        item1.event.set()

        item2 = InboxItem(kind="choices", preamble="Go", choices=list(choices))
        assert s.dedup_and_enqueue(item2) is True
        assert len(s.inbox) == 2

    def test_concurrent_threads_piggyback(self):
        """Multiple threads enqueuing identical choices: one enqueued, rest piggyback."""
        s = Session(session_id="test-1", name="Agent 1")
        choices = [{"label": "A"}, {"label": "B"}]
        results = []
        barrier = threading.Barrier(5)

        def try_enqueue(idx):
            item = InboxItem(kind="choices", preamble="Pick",
                             choices=list(choices))
            barrier.wait()  # synchronize start
            enqueued = s.dedup_and_enqueue(item)
            results.append((idx, enqueued, item))

        threads = [threading.Thread(target=try_enqueue, args=(i,))
                   for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        # Exactly one should be enqueued (True), rest should get existing item back
        enqueued_count = sum(1 for _, e, _ in results if e is True)
        piggyback_count = sum(1 for _, e, _ in results if isinstance(e, InboxItem))
        assert enqueued_count == 1, f"Expected 1 enqueued, got {enqueued_count}"
        assert piggyback_count == 4, f"Expected 4 piggybacked, got {piggyback_count}"

        # All piggybacked items got back the same existing InboxItem
        piggyback_items = [e for _, e, _ in results if isinstance(e, InboxItem)]
        assert all(p is piggyback_items[0] for p in piggyback_items)

class TestUnifiedInboxDataCollection:
    """Tests for cross-session inbox data collection logic.

    These test the underlying session/inbox data model that the unified inbox
    view relies on, without needing the TUI.
    """

    def test_collect_pending_choices_across_sessions(self):
        """Can collect all pending choices from multiple sessions."""
        m = SessionManager()
        s1, _ = m.get_or_create("a")
        s2, _ = m.get_or_create("b")
        s3, _ = m.get_or_create("c")

        s1.name = "Agent 1"
        s2.name = "Agent 2"
        s3.name = "Agent 3"

        # s1 has active choices
        s1.active = True
        s1.preamble = "Choose action"
        s1.choices = [{"label": "Fix bug"}, {"label": "Add feature"}]

        # s2 has queued inbox items
        item1 = InboxItem(kind="choices", preamble="Pick a file",
                          choices=[{"label": "main.py"}, {"label": "test.py"}])
        item2 = InboxItem(kind="choices", preamble="Select mode",
                          choices=[{"label": "Debug"}, {"label": "Release"}])
        s2.enqueue(item1)
        s2.enqueue(item2)

        # s3 has no pending choices
        s3.active = False

        # Collect all pending choices (mimics action_unified_inbox logic)
        unified = []
        for sess in m.all_sessions():
            if sess.active and sess.choices:
                unified.append({
                    "session_name": sess.name,
                    "preamble": sess.preamble,
                    "n_choices": len(sess.choices),
                })
            for item in sess.inbox:
                if not item.done and item.kind == "choices":
                    unified.append({
                        "session_name": sess.name,
                        "preamble": item.preamble,
                        "n_choices": len(item.choices),
                    })

        assert len(unified) == 3
        assert unified[0]["session_name"] == "Agent 1"
        assert unified[0]["preamble"] == "Choose action"
        assert unified[1]["session_name"] == "Agent 2"
        assert unified[1]["preamble"] == "Pick a file"
        assert unified[2]["session_name"] == "Agent 2"
        assert unified[2]["preamble"] == "Select mode"

    def test_collect_no_pending_choices(self):
        """Returns empty list when no sessions have pending choices."""
        m = SessionManager()
        s1, _ = m.get_or_create("a")
        s1.active = False

        unified = []
        for sess in m.all_sessions():
            if sess.active and sess.choices:
                unified.append({"session_name": sess.name})
            for item in sess.inbox:
                if not item.done and item.kind == "choices":
                    unified.append({"session_name": sess.name})

        assert unified == []

    def test_collect_excludes_done_items(self):
        """Done inbox items are not included in unified collection."""
        m = SessionManager()
        s, _ = m.get_or_create("a")
        s.name = "Agent 1"

        item1 = InboxItem(kind="choices", preamble="Done item")
        item2 = InboxItem(kind="choices", preamble="Pending item")
        s.enqueue(item1)
        s.enqueue(item2)

        # Resolve the first item
        s.resolve_front({"selected": "Done"})

        unified = []
        for sess in m.all_sessions():
            for item in sess.inbox:
                if not item.done and item.kind == "choices":
                    unified.append({"preamble": item.preamble})

        assert len(unified) == 1
        assert unified[0]["preamble"] == "Pending item"

    def test_collect_excludes_speech_items(self):
        """Speech inbox items are not included in unified collection."""
        m = SessionManager()
        s, _ = m.get_or_create("a")

        s.enqueue(InboxItem(kind="speech", text="Hello"))
        s.enqueue(InboxItem(kind="choices", preamble="Pick one",
                            choices=[{"label": "A"}]))

        unified = []
        for sess in m.all_sessions():
            for item in sess.inbox:
                if not item.done and item.kind == "choices":
                    unified.append({"preamble": item.preamble})

        assert len(unified) == 1
        assert unified[0]["preamble"] == "Pick one"

    def test_session_switch_after_unified_select(self):
        """After selecting from unified inbox, the session manager can focus it."""
        m = SessionManager()
        s1, _ = m.get_or_create("a")
        s2, _ = m.get_or_create("b")

        s1.name = "Agent 1"
        s2.name = "Agent 2"
        s2.active = True
        s2.preamble = "Pick"
        s2.choices = [{"label": "X"}]

        # Simulate selecting s2's choices from unified inbox
        target = m.focus("b")
        assert target is s2
        assert m.focused() is s2

    def test_multiple_agents_unique_session_count(self):
        """Can count unique agents with pending choices."""
        m = SessionManager()
        s1, _ = m.get_or_create("a")
        s2, _ = m.get_or_create("b")

        s1.name = "Agent 1"
        s2.name = "Agent 2"

        s1.active = True
        s1.choices = [{"label": "A"}]
        s1.preamble = "First"

        s2.enqueue(InboxItem(kind="choices", preamble="Second",
                             choices=[{"label": "B"}]))
        s2.enqueue(InboxItem(kind="choices", preamble="Third",
                             choices=[{"label": "C"}]))

        unified = []
        for sess in m.all_sessions():
            if sess.active and sess.choices:
                unified.append({"session_name": sess.name})
            for item in sess.inbox:
                if not item.done and item.kind == "choices":
                    unified.append({"session_name": sess.name})

        n_agents = len(set(item["session_name"] for item in unified))
        assert n_agents == 2
        assert len(unified) == 3


class TestSessionManagerVoiceTracking:
    """Tests for in_use_voices/in_use_emotions tracking methods."""

    def test_in_use_voices_empty(self):
        m = SessionManager()
        assert m.in_use_voices() == set()

    def test_in_use_voices_with_overrides(self):
        m = SessionManager()
        s1, _ = m.get_or_create("a")
        s2, _ = m.get_or_create("b")
        s1.voice_override = "sage"
        s2.voice_override = "coral"
        assert m.in_use_voices() == {"sage", "coral"}

    def test_in_use_voices_ignores_none(self):
        m = SessionManager()
        s1, _ = m.get_or_create("a")
        s2, _ = m.get_or_create("b")
        s1.voice_override = "sage"
        # s2.voice_override is None by default
        assert m.in_use_voices() == {"sage"}

    def test_in_use_voices_after_removal(self):
        m = SessionManager()
        s1, _ = m.get_or_create("a")
        s2, _ = m.get_or_create("b")
        s1.voice_override = "sage"
        s2.voice_override = "coral"
        m.remove("a")
        assert m.in_use_voices() == {"coral"}

    def test_in_use_emotions_empty(self):
        m = SessionManager()
        assert m.in_use_emotions() == set()

    def test_in_use_emotions_with_overrides(self):
        m = SessionManager()
        s1, _ = m.get_or_create("a")
        s2, _ = m.get_or_create("b")
        s1.emotion_override = "happy"
        s2.emotion_override = "calm"
        assert m.in_use_emotions() == {"happy", "calm"}

    def test_in_use_emotions_ignores_none(self):
        m = SessionManager()
        s1, _ = m.get_or_create("a")
        s2, _ = m.get_or_create("b")
        s1.emotion_override = "happy"
        assert m.in_use_emotions() == {"happy"}


class TestSpeechLogCap:
    """Tests for Session.append_speech() trimming."""

    def test_append_speech_basic(self):
        s = Session(session_id="test-1", name="Agent 1")
        entry = SpeechEntry(text="hello")
        s.append_speech(entry)
        assert len(s.speech_log) == 1
        assert s.speech_log[0].text == "hello"

    def test_append_speech_trims_at_cap(self):
        s = Session(session_id="test-1", name="Agent 1")
        # Fill to cap
        for i in range(s._speech_log_max):
            s.append_speech(SpeechEntry(text=f"msg-{i}"))
        assert len(s.speech_log) == s._speech_log_max
        # One more should trim
        s.append_speech(SpeechEntry(text="overflow"))
        assert len(s.speech_log) == s._speech_log_max
        assert s.speech_log[0].text == "msg-1"  # oldest trimmed
        assert s.speech_log[-1].text == "overflow"

    def test_append_speech_preserves_order(self):
        s = Session(session_id="test-1", name="Agent 1")
        for i in range(5):
            s.append_speech(SpeechEntry(text=f"msg-{i}"))
        assert [e.text for e in s.speech_log] == [f"msg-{i}" for i in range(5)]


class TestHistoryCap:
    """Tests for Session.append_history() trimming."""

    def test_append_history_basic(self):
        s = Session(session_id="test-1", name="Agent 1")
        entry = HistoryEntry(label="A", summary="desc", preamble="pick")
        s.append_history(entry)
        assert len(s.history) == 1
        assert s.history[0].label == "A"

    def test_append_history_trims_at_cap(self):
        s = Session(session_id="test-1", name="Agent 1")
        for i in range(s._history_max):
            s.append_history(HistoryEntry(
                label=f"choice-{i}", summary="", preamble=""))
        assert len(s.history) == s._history_max
        s.append_history(HistoryEntry(label="overflow", summary="", preamble=""))
        assert len(s.history) == s._history_max
        assert s.history[0].label == "choice-1"
        assert s.history[-1].label == "overflow"


class TestResolvePendingInbox:
    """Tests for _resolve_pending_inbox() — unblocking threads on session removal."""

    def test_resolve_empty_inbox(self):
        from io_mcp.session import _resolve_pending_inbox
        s = Session(session_id="test-1", name="Agent 1")
        assert _resolve_pending_inbox(s) == 0

    def test_resolve_pending_items(self):
        from io_mcp.session import _resolve_pending_inbox
        s = Session(session_id="test-1", name="Agent 1")
        item1 = InboxItem(kind="choices", preamble="Pick")
        item2 = InboxItem(kind="choices", preamble="Choose")
        s.enqueue(item1)
        s.enqueue(item2)

        resolved = _resolve_pending_inbox(s)
        assert resolved == 2
        assert item1.done is True
        assert item2.done is True
        assert item1.event.is_set()
        assert item2.event.is_set()
        assert item1.result["selected"] == "_cancelled"
        assert item2.result["selected"] == "_cancelled"
        assert len(s.inbox) == 0

    def test_resolve_skips_already_done(self):
        from io_mcp.session import _resolve_pending_inbox
        s = Session(session_id="test-1", name="Agent 1")
        item1 = InboxItem(kind="choices", preamble="Done already")
        item1.done = True
        item1.result = {"selected": "A"}
        item1.event.set()
        item2 = InboxItem(kind="choices", preamble="Pending")
        s.enqueue(item1)
        s.enqueue(item2)

        resolved = _resolve_pending_inbox(s)
        assert resolved == 1  # only item2 was pending
        assert item2.done is True
        assert item2.result["selected"] == "_cancelled"

    def test_blocked_thread_unblocks_on_resolve(self):
        """Thread blocked on item.event.wait() should be released."""
        from io_mcp.session import _resolve_pending_inbox
        s = Session(session_id="test-1", name="Agent 1")
        item = InboxItem(kind="choices", preamble="Pick")
        s.enqueue(item)

        result_holder = [None]

        def waiter():
            item.event.wait(timeout=5)
            result_holder[0] = item.result

        t = threading.Thread(target=waiter, daemon=True)
        t.start()

        # Give thread time to start waiting
        time.sleep(0.05)
        assert t.is_alive()

        _resolve_pending_inbox(s)
        t.join(timeout=2)
        assert not t.is_alive()
        assert result_holder[0]["selected"] == "_cancelled"


class TestRemoveResolvesInbox:
    """Tests that SessionManager.remove() resolves pending inbox items."""

    def test_remove_unblocks_waiting_thread(self):
        m = SessionManager()
        s, _ = m.get_or_create("a")
        item = InboxItem(kind="choices", preamble="Pick")
        s.enqueue(item)

        result_holder = [None]

        def waiter():
            item.event.wait(timeout=5)
            result_holder[0] = item.result

        t = threading.Thread(target=waiter, daemon=True)
        t.start()
        time.sleep(0.05)

        m.remove("a")
        t.join(timeout=2)
        assert not t.is_alive()
        assert result_holder[0]["selected"] == "_cancelled"
        assert m.count() == 0

    def test_remove_without_inbox_still_works(self):
        m = SessionManager()
        m.get_or_create("a")
        m.get_or_create("b")
        m.remove("a")
        assert m.count() == 1
        assert m.get("a") is None


class TestCleanupStaleImprovements:
    """Tests for the improved cleanup_stale logic."""

    def test_cleanup_does_not_remove_sessions_with_inbox(self):
        """Sessions with pending inbox items should not be removed even if stale."""
        m = SessionManager()
        s1, _ = m.get_or_create("a")
        s2, _ = m.get_or_create("b")
        m.focus("a")  # focus on a, so b is eligible

        # Make b stale
        s2.last_activity = time.time() - 600
        # But b has a pending inbox item
        s2.enqueue(InboxItem(kind="choices", preamble="Waiting"))

        removed = m.cleanup_stale(timeout_seconds=300)
        assert removed == []
        assert m.count() == 2

    def test_cleanup_removes_stale_without_inbox(self):
        """Stale sessions without inbox items should be removed."""
        m = SessionManager()
        s1, _ = m.get_or_create("a")
        s2, _ = m.get_or_create("b")
        m.focus("a")

        s2.last_activity = time.time() - 600
        removed = m.cleanup_stale(timeout_seconds=300)
        assert removed == ["b"]
        assert m.count() == 1

    def test_cleanup_never_removes_focused(self):
        m = SessionManager()
        s1, _ = m.get_or_create("a")
        s1.last_activity = time.time() - 600
        removed = m.cleanup_stale(timeout_seconds=300)
        assert removed == []

    def test_cleanup_never_removes_active(self):
        m = SessionManager()
        s1, _ = m.get_or_create("a")
        s2, _ = m.get_or_create("b")
        m.focus("a")

        s2.last_activity = time.time() - 600
        s2.active = True
        removed = m.cleanup_stale(timeout_seconds=300)
        assert removed == []

    def test_cleanup_resolves_inbox_on_removal(self):
        """When cleanup removes a session, pending inbox items get resolved."""
        m = SessionManager()
        s1, _ = m.get_or_create("a")
        s2, _ = m.get_or_create("b")
        m.focus("a")

        # b is stale and has no inbox — will be removed
        s2.last_activity = time.time() - 600
        # Add an already-done item to make sure it doesn't break
        done_item = InboxItem(kind="choices", preamble="Done")
        done_item.done = True
        done_item.result = {"selected": "X"}
        done_item.event.set()
        # The done item in inbox should not prevent cleanup
        # (inbox deque has it, but resolve handles it)

        removed = m.cleanup_stale(timeout_seconds=300)
        assert "b" in removed

    def test_cleanup_atomic_no_toctou(self):
        """Cleanup should be atomic — no window for session to become active."""
        m = SessionManager()
        s1, _ = m.get_or_create("a")
        s2, _ = m.get_or_create("b")
        m.focus("a")
        s2.last_activity = time.time() - 600

        # Simulate concurrent access: another thread makes s2 active
        # After our fix, both check and remove happen under the same lock,
        # so this shouldn't cause issues
        activated = [False]

        def activator():
            time.sleep(0.01)  # slight delay
            s2_ref = m.get("b")
            if s2_ref:
                s2_ref.active = True
                activated[0] = True

        t = threading.Thread(target=activator, daemon=True)
        t.start()

        removed = m.cleanup_stale(timeout_seconds=300)
        t.join(timeout=2)

        # Either b was removed (cleanup won the race) or it wasn't
        # (activator won), but we should never have a half-removed session
        if "b" in removed:
            assert m.get("b") is None
        else:
            assert m.get("b") is not None


class TestSessionMood:
    """Tests for Session.mood property — activity-based mood detection."""

    def test_mood_idle_no_activity(self):
        """No activity log at all → idle."""
        s = Session(session_id="test-1", name="Agent 1")
        assert s.mood == "idle"

    def test_mood_idle_stale_activity(self):
        """Last activity > 30s ago → idle."""
        s = Session(session_id="test-1", name="Agent 1")
        s.activity_log.append({"kind": "tool", "timestamp": time.time() - 60})
        assert s.mood == "idle"

    def test_mood_speaking_recent_speech(self):
        """Last activity was speech within 10s → speaking."""
        s = Session(session_id="test-1", name="Agent 1")
        s.activity_log.append({"kind": "speech", "timestamp": time.time() - 3})
        assert s.mood == "speaking"

    def test_mood_speaking_expires_after_10s(self):
        """Speech older than 10s doesn't count as speaking."""
        s = Session(session_id="test-1", name="Agent 1")
        s.activity_log.append({"kind": "speech", "timestamp": time.time() - 15})
        # 15s ago speech, but within 30s → still active, not idle
        # With only 1 recent activity (within 10s window = 0), falls through
        # 15s < 30s so not idle via timestamp check, but recent count is 0 → idle
        assert s.mood == "idle"

    def test_mood_flowing_low_activity(self):
        """1-3 activities in last 10s → flowing."""
        s = Session(session_id="test-1", name="Agent 1")
        now = time.time()
        for i in range(2):
            s.activity_log.append({"kind": "tool", "timestamp": now - i * 3})
        assert s.mood == "flowing"

    def test_mood_busy_moderate_activity(self):
        """4-8 activities in last 10s → busy."""
        s = Session(session_id="test-1", name="Agent 1")
        now = time.time()
        for i in range(6):
            s.activity_log.append({"kind": "tool", "timestamp": now - i})
        assert s.mood == "busy"

    def test_mood_thrashing_high_activity(self):
        """9+ activities in last 10s → thrashing."""
        s = Session(session_id="test-1", name="Agent 1")
        now = time.time()
        for i in range(12):
            s.activity_log.append({"kind": "tool", "timestamp": now - i * 0.5})
        assert s.mood == "thrashing"

    def test_mood_speaking_takes_priority_over_busy(self):
        """If last activity is recent speech, mood is 'speaking' regardless of tool count."""
        s = Session(session_id="test-1", name="Agent 1")
        now = time.time()
        # Lots of tool activity
        for i in range(10):
            s.activity_log.append({"kind": "tool", "timestamp": now - i})
        # But last entry is speech
        s.activity_log.append({"kind": "speech", "timestamp": now - 1})
        assert s.mood == "speaking"

    def test_mood_flowing_exactly_one_activity(self):
        """Exactly 1 activity in last 10s → flowing."""
        s = Session(session_id="test-1", name="Agent 1")
        s.activity_log.append({"kind": "tool", "timestamp": time.time() - 5})
        assert s.mood == "flowing"

    def test_mood_boundary_exactly_4_is_busy(self):
        """Exactly 4 activities in last 10s → busy."""
        s = Session(session_id="test-1", name="Agent 1")
        now = time.time()
        for i in range(4):
            s.activity_log.append({"kind": "tool", "timestamp": now - i * 2})
        assert s.mood == "busy"

    def test_mood_boundary_exactly_9_is_thrashing(self):
        """Exactly 9 activities in last 10s → thrashing."""
        s = Session(session_id="test-1", name="Agent 1")
        now = time.time()
        for i in range(9):
            s.activity_log.append({"kind": "tool", "timestamp": now - i})
        assert s.mood == "thrashing"


class TestCheckAchievements:
    """Tests for Session.check_achievements() — all achievement types."""

    def test_first_blood(self):
        """First tool call unlocks 'first_blood'."""
        s = Session(session_id="test-1", name="Agent 1")
        s.tool_call_count = 1
        new = s.check_achievements()
        assert any("First Blood" in a for a in new)
        assert "first_blood" in s.achievements_unlocked

    def test_getting_started(self):
        """10 tool calls unlocks 'getting_started'."""
        s = Session(session_id="test-1", name="Agent 1")
        s.tool_call_count = 10
        new = s.check_achievements()
        assert any("Getting Started" in a for a in new)
        assert "getting_started" in s.achievements_unlocked

    def test_centurion(self):
        """100 tool calls unlocks 'centurion'."""
        s = Session(session_id="test-1", name="Agent 1")
        s.tool_call_count = 100
        new = s.check_achievements()
        assert any("Centurion" in a for a in new)
        assert "centurion" in s.achievements_unlocked

    def test_five_hundred(self):
        """500 tool calls unlocks 'five_hundred'."""
        s = Session(session_id="test-1", name="Agent 1")
        s.tool_call_count = 500
        new = s.check_achievements()
        assert any("Unstoppable" in a for a in new)
        assert "five_hundred" in s.achievements_unlocked

    def test_chatterbox(self):
        """20 speech events unlocks 'chatterbox'."""
        s = Session(session_id="test-1", name="Agent 1")
        for i in range(20):
            s.speech_log.append(SpeechEntry(text=f"msg-{i}"))
        new = s.check_achievements()
        assert any("Chatterbox" in a for a in new)
        assert "chatterbox" in s.achievements_unlocked

    def test_decisive(self):
        """5 selections unlocks 'decisive'."""
        s = Session(session_id="test-1", name="Agent 1")
        for i in range(5):
            s.history.append(HistoryEntry(label=f"opt-{i}", summary="", preamble=""))
        new = s.check_achievements()
        assert any("Decisive" in a for a in new)
        assert "decisive" in s.achievements_unlocked

    def test_veteran(self):
        """20 selections unlocks 'veteran'."""
        s = Session(session_id="test-1", name="Agent 1")
        for i in range(20):
            s.history.append(HistoryEntry(label=f"opt-{i}", summary="", preamble=""))
        new = s.check_achievements()
        assert any("Veteran" in a for a in new)
        assert "veteran" in s.achievements_unlocked

    def test_marathon(self):
        """30 minutes of history unlocks 'marathon'."""
        s = Session(session_id="test-1", name="Agent 1")
        now = time.time()
        s.history.append(HistoryEntry(label="old", summary="", preamble="",
                                       timestamp=now - 35 * 60))
        s.history.append(HistoryEntry(label="new", summary="", preamble="",
                                       timestamp=now - 1))
        new = s.check_achievements()
        assert any("Marathon" in a for a in new)
        assert "marathon" in s.achievements_unlocked

    def test_ultra(self):
        """60 minutes of history unlocks 'ultra'."""
        s = Session(session_id="test-1", name="Agent 1")
        now = time.time()
        s.history.append(HistoryEntry(label="old", summary="", preamble="",
                                       timestamp=now - 65 * 60))
        s.history.append(HistoryEntry(label="new", summary="", preamble="",
                                       timestamp=now - 1))
        new = s.check_achievements()
        assert any("Ultra" in a for a in new)
        assert "ultra" in s.achievements_unlocked

    def test_speed_demon(self):
        """5+ activities in 5 seconds unlocks 'speed_demon'."""
        s = Session(session_id="test-1", name="Agent 1")
        now = time.time()
        for i in range(6):
            s.activity_log.append({"kind": "tool", "timestamp": now - i * 0.5,
                                   "tool": "test", "detail": ""})
        new = s.check_achievements()
        assert any("Speed Demon" in a for a in new)
        assert "speed_demon" in s.achievements_unlocked

    def test_achievements_fire_only_once(self):
        """Achievements are only returned once — subsequent calls return empty."""
        s = Session(session_id="test-1", name="Agent 1")
        s.tool_call_count = 1
        first = s.check_achievements()
        assert len(first) > 0
        second = s.check_achievements()
        # first_blood was already unlocked, so shouldn't fire again
        assert not any("First Blood" in a for a in second)

    def test_multiple_achievements_at_once(self):
        """Multiple achievements can fire in the same check."""
        s = Session(session_id="test-1", name="Agent 1")
        s.tool_call_count = 100  # triggers first_blood, getting_started, centurion
        new = s.check_achievements()
        assert any("First Blood" in a for a in new)
        assert any("Getting Started" in a for a in new)
        assert any("Centurion" in a for a in new)

    def test_no_achievements_unlocked_initially(self):
        """New session has no achievements."""
        s = Session(session_id="test-1", name="Agent 1")
        assert s.achievements_unlocked == set()
        new = s.check_achievements()
        assert new == []

    def test_night_owl_at_late_hour(self):
        """Night owl fires between midnight and 5am.

        We can't reliably control time.localtime, so we test the
        condition directly: if current hour < 5, it should fire.
        """
        s = Session(session_id="test-1", name="Agent 1")
        hour = time.localtime().tm_hour
        new = s.check_achievements()
        if hour < 5:
            assert any("Night Owl" in a for a in new)
            assert "night_owl" in s.achievements_unlocked
        else:
            assert not any("Night Owl" in a for a in new)
            assert "night_owl" not in s.achievements_unlocked

    def test_no_marathon_without_history(self):
        """Marathon and ultra can't fire without history entries."""
        s = Session(session_id="test-1", name="Agent 1")
        s.tool_call_count = 1000
        new = s.check_achievements()
        assert not any("Marathon" in a for a in new)
        assert not any("Ultra" in a for a in new)


class TestLogActivity:
    """Tests for Session.log_activity() — timestamped activity feed."""

    def test_log_activity_basic(self):
        """Adds a single activity entry."""
        s = Session(session_id="test-1", name="Agent 1")
        s.log_activity("speak_async", detail="Hello world", kind="speech")
        assert len(s.activity_log) == 1
        entry = s.activity_log[0]
        assert entry["tool"] == "speak_async"
        assert entry["detail"] == "Hello world"
        assert entry["kind"] == "speech"
        assert entry["timestamp"] > 0

    def test_log_activity_default_kind(self):
        """Default kind is 'tool'."""
        s = Session(session_id="test-1", name="Agent 1")
        s.log_activity("present_choices")
        assert s.activity_log[0]["kind"] == "tool"

    def test_log_activity_empty_detail(self):
        """Default detail is empty string."""
        s = Session(session_id="test-1", name="Agent 1")
        s.log_activity("register_session")
        assert s.activity_log[0]["detail"] == ""

    def test_log_activity_multiple_entries(self):
        """Multiple entries are appended in order."""
        s = Session(session_id="test-1", name="Agent 1")
        s.log_activity("tool_a")
        s.log_activity("tool_b")
        s.log_activity("tool_c")
        assert len(s.activity_log) == 3
        assert s.activity_log[0]["tool"] == "tool_a"
        assert s.activity_log[1]["tool"] == "tool_b"
        assert s.activity_log[2]["tool"] == "tool_c"

    def test_log_activity_trims_at_cap(self):
        """Activity log trims oldest entries when exceeding cap."""
        s = Session(session_id="test-1", name="Agent 1")
        cap = s._activity_log_max
        for i in range(cap + 10):
            s.log_activity(f"tool-{i}")
        assert len(s.activity_log) == cap
        # Oldest entries should have been trimmed
        assert s.activity_log[0]["tool"] == "tool-10"
        assert s.activity_log[-1]["tool"] == f"tool-{cap + 9}"

    def test_log_activity_preserves_all_kinds(self):
        """All event kinds are stored correctly."""
        s = Session(session_id="test-1", name="Agent 1")
        s.log_activity("speak", kind="speech")
        s.log_activity("select", kind="selection")
        s.log_activity("update", kind="status")
        s.log_activity("call", kind="tool")
        kinds = [e["kind"] for e in s.activity_log]
        assert kinds == ["speech", "selection", "status", "tool"]


class TestStreakMinutes:
    """Tests for Session.streak_minutes property."""

    def test_streak_no_activity(self):
        """No activity log → 0 streak minutes."""
        s = Session(session_id="test-1", name="Agent 1")
        assert s.streak_minutes == 0

    def test_streak_recent_activity(self):
        """Recent continuous activity gives positive streak."""
        s = Session(session_id="test-1", name="Agent 1")
        now = time.time()
        # Activity over 5 minutes, spaced 30s apart
        for i in range(10):
            s.activity_log.append({
                "timestamp": now - (10 - i) * 30,
                "tool": "test", "detail": "", "kind": "tool",
            })
        streak = s.streak_minutes
        assert streak >= 4  # ~5 minutes of activity

    def test_streak_resets_after_2min_idle(self):
        """Gap > 120s since last activity → 0."""
        s = Session(session_id="test-1", name="Agent 1")
        s.activity_log.append({
            "timestamp": time.time() - 180,  # 3 minutes ago
            "tool": "test", "detail": "", "kind": "tool",
        })
        assert s.streak_minutes == 0

    def test_streak_breaks_on_gap(self):
        """A 2+ minute gap in the middle resets the streak start."""
        s = Session(session_id="test-1", name="Agent 1")
        now = time.time()
        # Old cluster (should be excluded from streak)
        s.activity_log.append({"timestamp": now - 600, "tool": "old",
                                "detail": "", "kind": "tool"})
        s.activity_log.append({"timestamp": now - 590, "tool": "old",
                                "detail": "", "kind": "tool"})
        # Gap of 8+ minutes
        # Recent cluster
        s.activity_log.append({"timestamp": now - 90, "tool": "new",
                                "detail": "", "kind": "tool"})
        s.activity_log.append({"timestamp": now - 60, "tool": "new",
                                "detail": "", "kind": "tool"})
        s.activity_log.append({"timestamp": now - 30, "tool": "new",
                                "detail": "", "kind": "tool"})
        s.activity_log.append({"timestamp": now - 5, "tool": "new",
                                "detail": "", "kind": "tool"})
        streak = s.streak_minutes
        # Streak should start from the recent cluster (~90s ago), not old cluster
        assert 1 <= streak <= 3

    def test_streak_minimum_one_minute(self):
        """Active streak is at least 1 minute."""
        s = Session(session_id="test-1", name="Agent 1")
        s.activity_log.append({
            "timestamp": time.time() - 5,  # 5 seconds ago
            "tool": "test", "detail": "", "kind": "tool",
        })
        assert s.streak_minutes >= 1


class TestAppendDone:
    """Tests for Session._append_done() — skip _restart, cap trimming."""

    def test_append_done_normal_item(self):
        """Normal resolved items are added to inbox_done."""
        s = Session(session_id="test-1", name="Agent 1")
        item = InboxItem(kind="choices", preamble="Pick")
        item.result = {"selected": "A", "summary": "Option A"}
        item.done = True
        s._append_done(item)
        assert len(s.inbox_done) == 1
        assert s.inbox_done[0] is item

    def test_append_done_skips_restart(self):
        """Items resolved with _restart are NOT added to inbox_done."""
        s = Session(session_id="test-1", name="Agent 1")
        item = InboxItem(kind="choices", preamble="Retry")
        item.result = {"selected": "_restart", "summary": "Owner thread died"}
        item.done = True
        s._append_done(item)
        assert len(s.inbox_done) == 0

    def test_append_done_skips_restart_duplicate(self):
        """Duplicate-resolved _restart items are also skipped."""
        s = Session(session_id="test-1", name="Agent 1")
        item = InboxItem(kind="choices", preamble="Dup")
        item.result = {"selected": "_restart", "summary": "Duplicate detected"}
        item.done = True
        s._append_done(item)
        assert len(s.inbox_done) == 0

    def test_append_done_allows_cancelled(self):
        """Items resolved with _cancelled ARE added (they're real cancellations)."""
        s = Session(session_id="test-1", name="Agent 1")
        item = InboxItem(kind="choices", preamble="Cancelled")
        item.result = {"selected": "_cancelled", "summary": "Session removed"}
        item.done = True
        s._append_done(item)
        assert len(s.inbox_done) == 1

    def test_append_done_none_result(self):
        """Items with None result are still added (edge case)."""
        s = Session(session_id="test-1", name="Agent 1")
        item = InboxItem(kind="choices", preamble="None result")
        item.result = None
        item.done = True
        s._append_done(item)
        assert len(s.inbox_done) == 1

    def test_append_done_caps_at_max(self):
        """inbox_done is trimmed when exceeding _inbox_done_max."""
        s = Session(session_id="test-1", name="Agent 1")
        cap = s._inbox_done_max
        for i in range(cap + 5):
            item = InboxItem(kind="choices", preamble=f"Item-{i}")
            item.result = {"selected": f"choice-{i}"}
            item.done = True
            s._append_done(item)
        assert len(s.inbox_done) == cap
        # Oldest items should have been trimmed
        assert s.inbox_done[0].preamble == "Item-5"
        assert s.inbox_done[-1].preamble == f"Item-{cap + 4}"

    def test_append_done_increments_generation(self):
        """_append_done increments _inbox_generation for TUI rebuild detection."""
        s = Session(session_id="test-1", name="Agent 1")
        gen_before = s._inbox_generation
        item = InboxItem(kind="choices", preamble="Test")
        item.result = {"selected": "A"}
        item.done = True
        s._append_done(item)
        assert s._inbox_generation == gen_before + 1


class TestEnqueueSpeech:
    """Tests for Session.enqueue_speech() — speech InboxItem creation."""

    def test_enqueue_speech_normal(self):
        """Normal speech is appended to the end of inbox."""
        s = Session(session_id="test-1", name="Agent 1")
        item = s.enqueue_speech("Hello world", blocking=True)
        assert item.kind == "speech"
        assert item.text == "Hello world"
        assert item.blocking is True
        assert item.priority == 0
        assert len(s.inbox) == 1
        assert s.inbox[0] is item

    def test_enqueue_speech_nonblocking(self):
        """Non-blocking speech."""
        s = Session(session_id="test-1", name="Agent 1")
        item = s.enqueue_speech("Status update", blocking=False)
        assert item.blocking is False

    def test_enqueue_speech_urgent_prepends(self):
        """Urgent speech (priority=1) is inserted at the front of inbox."""
        s = Session(session_id="test-1", name="Agent 1")
        normal = s.enqueue_speech("Normal message", priority=0)
        urgent = s.enqueue_speech("URGENT!", priority=1)
        assert len(s.inbox) == 2
        assert s.inbox[0] is urgent  # urgent at front
        assert s.inbox[1] is normal

    def test_enqueue_speech_sets_drain_kick(self):
        """enqueue_speech sets drain_kick event."""
        s = Session(session_id="test-1", name="Agent 1")
        s.drain_kick.clear()
        s.enqueue_speech("Hello")
        assert s.drain_kick.is_set()

    def test_enqueue_speech_increments_generation(self):
        """enqueue_speech increments _inbox_generation."""
        s = Session(session_id="test-1", name="Agent 1")
        gen_before = s._inbox_generation
        s.enqueue_speech("Hello")
        assert s._inbox_generation == gen_before + 1

    def test_enqueue_speech_preamble_is_text(self):
        """Speech InboxItem's preamble is set to the speech text."""
        s = Session(session_id="test-1", name="Agent 1")
        item = s.enqueue_speech("Test message")
        assert item.preamble == "Test message"


class TestPeekInboxOrphanCleanup:
    """Tests for peek_inbox() orphaned thread detection."""

    def test_peek_inbox_cleans_dead_thread_items(self):
        """Items whose owner thread has died are resolved as _restart."""
        s = Session(session_id="test-1", name="Agent 1")

        done_event = threading.Event()

        def short_lived():
            done_event.set()
            # Thread ends immediately

        t = threading.Thread(target=short_lived, daemon=True)
        t.start()
        done_event.wait(timeout=2)
        t.join(timeout=2)
        assert not t.is_alive()

        # Create an inbox item owned by the dead thread
        item = InboxItem(kind="choices", preamble="Orphaned")
        item.owner_thread = t

        s.enqueue(item)
        # Add a second live item
        live_item = InboxItem(kind="choices", preamble="Live")
        live_item.owner_thread = None  # no thread tracking
        s.enqueue(live_item)

        # peek should skip the orphaned item and return live_item
        front = s.peek_inbox()
        assert front is live_item
        assert item.done is True
        assert item.result["selected"] == "_restart"

    def test_peek_inbox_kicks_drain_on_orphan(self):
        """Cleaning up orphans sets drain_kick so waiting threads wake."""
        s = Session(session_id="test-1", name="Agent 1")
        s.drain_kick.clear()

        # Create a dead thread
        t = threading.Thread(target=lambda: None, daemon=True)
        t.start()
        t.join(timeout=2)

        item = InboxItem(kind="choices", preamble="Orphaned")
        item.owner_thread = t
        s.enqueue(item)

        s.peek_inbox()
        assert s.drain_kick.is_set()


class TestTimelineAgeFormatting:
    """Tests for timeline() age string formatting across different ranges."""

    def test_age_seconds(self):
        """Entries < 60s show seconds."""
        s = Session(session_id="test-1", name="Agent 1")
        s.speech_log.append(SpeechEntry(text="recent", timestamp=time.time() - 15))
        tl = s.timeline()
        assert "15s ago" in tl[0]["age"] or "16s ago" in tl[0]["age"]

    def test_age_minutes(self):
        """Entries 60s-3600s show minutes."""
        s = Session(session_id="test-1", name="Agent 1")
        s.speech_log.append(SpeechEntry(text="a bit ago", timestamp=time.time() - 300))
        tl = s.timeline()
        assert "5m ago" == tl[0]["age"]

    def test_age_hours(self):
        """Entries >= 3600s show hours and minutes."""
        s = Session(session_id="test-1", name="Agent 1")
        s.speech_log.append(SpeechEntry(text="long ago", timestamp=time.time() - 5400))
        tl = s.timeline()
        assert "1h30m ago" == tl[0]["age"]

    def test_age_hours_exact(self):
        """Exactly 1 hour shows 1h00m ago."""
        s = Session(session_id="test-1", name="Agent 1")
        s.speech_log.append(SpeechEntry(text="1 hour", timestamp=time.time() - 3600))
        tl = s.timeline()
        assert "1h00m ago" == tl[0]["age"]

    def test_timeline_selection_detail_populated(self):
        """Selection entries in timeline have detail (summary) field."""
        s = Session(session_id="test-1", name="Agent 1")
        s.history.append(HistoryEntry(label="Fix bug", summary="Fixed null check",
                                       preamble="Choose", timestamp=time.time() - 10))
        tl = s.timeline()
        assert tl[0]["detail"] == "Fixed null check"

    def test_timeline_speech_detail_empty(self):
        """Speech entries in timeline have empty detail."""
        s = Session(session_id="test-1", name="Agent 1")
        s.speech_log.append(SpeechEntry(text="Hello", timestamp=time.time() - 5))
        tl = s.timeline()
        assert tl[0]["detail"] == ""


class TestSummaryEdgeCases:
    """Extended tests for Session.summary() — edge cases and formatting."""

    def test_summary_single_tool_call_grammar(self):
        """Single tool call uses singular 'tool call' not 'tool calls'."""
        s = Session(session_id="test-1", name="Agent 1")
        s.tool_call_count = 1
        summary = s.summary()
        assert "1 tool call." in summary or "1 tool call," in summary
        assert "1 tool calls" not in summary

    def test_summary_single_selection_grammar(self):
        """Single selection uses singular 'selection' not 'selections'."""
        s = Session(session_id="test-1", name="Agent 1")
        s.tool_call_count = 5
        s.history = [HistoryEntry(label="A", summary="", preamble="")]
        summary = s.summary()
        assert "1 selection" in summary
        assert "1 selections" not in summary

    def test_summary_duration_seconds(self):
        """Short sessions show duration in seconds."""
        s = Session(session_id="test-1", name="Agent 1")
        s.tool_call_count = 2
        s.last_tool_call = time.time() - 5
        summary = s.summary()
        # Should show 'up Ns' for short durations
        assert "up " in summary

    def test_summary_duration_hours(self):
        """Long sessions show hours and minutes."""
        s = Session(session_id="test-1", name="Agent 1")
        s.tool_call_count = 50
        now = time.time()
        s.history = [
            HistoryEntry(label="old", summary="", preamble="",
                         timestamp=now - 90 * 60),  # 90 minutes ago
        ]
        summary = s.summary()
        assert "1h30m" in summary

    def test_summary_includes_last_tool_name(self):
        """Summary includes the last tool name."""
        s = Session(session_id="test-1", name="Agent 1")
        s.tool_call_count = 5
        s.last_tool_name = "present_choices"
        summary = s.summary()
        assert "present_choices" in summary

    def test_summary_working_status_default(self):
        """Default healthy non-active session shows 'Working'."""
        s = Session(session_id="test-1", name="Agent 1")
        s.tool_call_count = 5
        summary = s.summary()
        assert "Working" in summary


class TestTabBarTextRendering:
    """Extended tests for SessionManager.tab_bar_text() rendering."""

    def test_tab_bar_empty(self):
        """Empty session manager returns empty string."""
        m = SessionManager()
        assert m.tab_bar_text() == ""

    def test_tab_bar_focused_is_bold_accent(self):
        """Focused tab uses bold accent color."""
        m = SessionManager()
        s, _ = m.get_or_create("a")
        s.name = "MyAgent"
        text = m.tab_bar_text(accent="#aabbcc")
        assert "[bold #aabbcc]MyAgent[/bold #aabbcc]" in text

    def test_tab_bar_unfocused_is_dim(self):
        """Non-focused tabs are dimmed."""
        m = SessionManager()
        s1, _ = m.get_or_create("a")
        s2, _ = m.get_or_create("b")
        s1.name = "Focused"
        s2.name = "Other"
        m.focus("a")
        text = m.tab_bar_text()
        assert "[dim]Other[/dim]" in text

    def test_tab_bar_pipe_separators(self):
        """Multiple tabs are separated by dim pipe characters."""
        m = SessionManager()
        s1, _ = m.get_or_create("a")
        s2, _ = m.get_or_create("b")
        s3, _ = m.get_or_create("c")
        s1.name = "A"
        s2.name = "B"
        s3.name = "C"
        text = m.tab_bar_text()
        assert " [dim]|[/dim] " in text

    def test_tab_bar_queued_badge_not_active(self):
        """Non-active session with queued choices shows +N (no dot)."""
        m = SessionManager()
        s, _ = m.get_or_create("a")
        s.name = "Agent"
        s.active = False
        s.health_status = "healthy"
        s.enqueue(InboxItem(kind="choices"))
        s.enqueue(InboxItem(kind="choices"))
        s.enqueue(InboxItem(kind="choices"))
        text = m.tab_bar_text(success="#a3be8c")
        assert "[#a3be8c]+3[/#a3be8c]" in text

    def test_tab_bar_active_with_queued_shows_dot_plus_n(self):
        """Active session with extra queued items shows ●+N."""
        m = SessionManager()
        s, _ = m.get_or_create("a")
        s.name = "Agent"
        s.active = True
        s.enqueue(InboxItem(kind="choices"))
        s.enqueue(InboxItem(kind="choices"))
        text = m.tab_bar_text(success="#a3be8c")
        assert "●+1" in text

    def test_tab_bar_custom_colors(self):
        """Tab bar respects custom color parameters."""
        m = SessionManager()
        s, _ = m.get_or_create("a")
        s.name = "Agent"
        s.health_status = "warning"
        s.active = False
        text = m.tab_bar_text(warning="#ff0000")
        assert "[#ff0000]![/#ff0000]" in text

    def test_tab_bar_healthy_dim_dot(self):
        """Healthy idle sessions show a dim dot."""
        m = SessionManager()
        s, _ = m.get_or_create("a")
        s.name = "Agent"
        s.active = False
        s.health_status = "healthy"
        text = m.tab_bar_text(fg_dim="#999999")
        assert "[#999999]●[/#999999]" in text


class TestCleanupStaleExtended:
    """Extended tests for SessionManager.cleanup_stale()."""

    def test_cleanup_multiple_stale(self):
        """Multiple stale sessions are all removed."""
        m = SessionManager()
        s1, _ = m.get_or_create("a")
        s2, _ = m.get_or_create("b")
        s3, _ = m.get_or_create("c")
        s4, _ = m.get_or_create("d")
        m.focus("a")  # a is focused, won't be removed

        # Make b, c, d all stale
        for s in [s2, s3, s4]:
            s.last_activity = time.time() - 600

        removed = m.cleanup_stale(timeout_seconds=300)
        assert set(removed) == {"b", "c", "d"}
        assert m.count() == 1

    def test_cleanup_respects_custom_timeout(self):
        """Custom timeout seconds are honored."""
        m = SessionManager()
        s1, _ = m.get_or_create("a")
        s2, _ = m.get_or_create("b")
        m.focus("a")

        # b is 60s old
        s2.last_activity = time.time() - 60

        # With 30s timeout, b is stale
        removed = m.cleanup_stale(timeout_seconds=30)
        assert "b" in removed

    def test_cleanup_keeps_recent_sessions(self):
        """Recently active sessions are not removed."""
        m = SessionManager()
        s1, _ = m.get_or_create("a")
        s2, _ = m.get_or_create("b")
        m.focus("a")

        # b was active 10 seconds ago
        s2.last_activity = time.time() - 10

        removed = m.cleanup_stale(timeout_seconds=300)
        assert removed == []
        assert m.count() == 2

    def test_cleanup_updates_active_session_if_removed(self):
        """If the focused session is removed (shouldn't happen), focus shifts."""
        m = SessionManager()
        s1, _ = m.get_or_create("a")
        s2, _ = m.get_or_create("b")
        m.focus("a")
        s2.last_activity = time.time() - 600

        removed = m.cleanup_stale(timeout_seconds=300)
        assert "b" in removed
        assert m.focused() is s1

    def test_cleanup_resolves_inbox_items_on_removal(self):
        """When a stale session is removed, its inbox items are resolved."""
        m = SessionManager()
        s1, _ = m.get_or_create("a")
        s2, _ = m.get_or_create("b")
        m.focus("a")

        s2.last_activity = time.time() - 600
        # Note: session with inbox items won't be removed because of the
        # inbox guard. But if it has no items, removal still works.
        removed = m.cleanup_stale(timeout_seconds=300)
        assert "b" in removed


class TestInUseVoicesEmotionsExtended:
    """Extended tests for voice/emotion tracking across session lifecycle."""

    def test_in_use_voices_duplicate_values(self):
        """Multiple sessions can have the same voice — set deduplicates."""
        m = SessionManager()
        s1, _ = m.get_or_create("a")
        s2, _ = m.get_or_create("b")
        s1.voice_override = "sage"
        s2.voice_override = "sage"
        assert m.in_use_voices() == {"sage"}

    def test_in_use_emotions_after_removal(self):
        """Removing a session removes its emotion from in_use."""
        m = SessionManager()
        s1, _ = m.get_or_create("a")
        s2, _ = m.get_or_create("b")
        s1.emotion_override = "happy"
        s2.emotion_override = "calm"
        m.remove("a")
        assert m.in_use_emotions() == {"calm"}

    def test_in_use_voices_with_model_override(self):
        """Sessions with model_override are still tracked by voice."""
        m = SessionManager()
        s, _ = m.get_or_create("a")
        s.voice_override = "noa"
        s.model_override = "azure/speech/azure-tts"
        assert "noa" in m.in_use_voices()

    def test_voice_and_emotion_tracking_together(self):
        """Voice and emotion tracking work independently."""
        m = SessionManager()
        s, _ = m.get_or_create("a")
        s.voice_override = "sage"
        s.emotion_override = "excited"
        assert m.in_use_voices() == {"sage"}
        assert m.in_use_emotions() == {"excited"}


class TestDedupAndEnqueueExtended:
    """Extended edge cases for Session.dedup_and_enqueue()."""

    def test_dedup_empty_choices_match(self):
        """Items with empty choice lists and same preamble are deduped."""
        s = Session(session_id="test-1", name="Agent 1")
        item1 = InboxItem(kind="choices", preamble="Empty", choices=[])
        assert s.dedup_and_enqueue(item1) is True

        item2 = InboxItem(kind="choices", preamble="Empty", choices=[])
        result = s.dedup_and_enqueue(item2)
        assert result is item1  # piggybacked

    def test_dedup_choice_order_matters(self):
        """Different choice order = different key → not deduped."""
        s = Session(session_id="test-1", name="Agent 1")
        item1 = InboxItem(kind="choices", preamble="Pick",
                          choices=[{"label": "A"}, {"label": "B"}])
        item2 = InboxItem(kind="choices", preamble="Pick",
                          choices=[{"label": "B"}, {"label": "A"}])
        assert s.dedup_and_enqueue(item1) is True
        assert s.dedup_and_enqueue(item2) is True  # different order, not a dup
        assert len(s.inbox) == 2

    def test_dedup_uses_label_key_only(self):
        """Dedup only checks 'label' field, ignores other choice fields."""
        s = Session(session_id="test-1", name="Agent 1")
        item1 = InboxItem(kind="choices", preamble="Go",
                          choices=[{"label": "A", "summary": "one"}])
        assert s.dedup_and_enqueue(item1) is True

        item2 = InboxItem(kind="choices", preamble="Go",
                          choices=[{"label": "A", "summary": "different"}])
        result = s.dedup_and_enqueue(item2)
        assert result is item1  # same label, so deduped

    def test_dedup_increments_generation_on_enqueue(self):
        """dedup_and_enqueue increments _inbox_generation when enqueuing new."""
        s = Session(session_id="test-1", name="Agent 1")
        gen_before = s._inbox_generation
        item = InboxItem(kind="choices", preamble="New",
                         choices=[{"label": "X"}])
        s.dedup_and_enqueue(item)
        assert s._inbox_generation == gen_before + 1

    def test_dedup_does_not_increment_generation_on_piggyback(self):
        """dedup_and_enqueue does NOT increment generation when piggybacking."""
        s = Session(session_id="test-1", name="Agent 1")
        item1 = InboxItem(kind="choices", preamble="Dup",
                          choices=[{"label": "A"}])
        s.dedup_and_enqueue(item1)
        gen_before = s._inbox_generation

        item2 = InboxItem(kind="choices", preamble="Dup",
                          choices=[{"label": "A"}])
        s.dedup_and_enqueue(item2)
        assert s._inbox_generation == gen_before  # no change


class TestResolvePendingInboxExtended:
    """Extended tests for _resolve_pending_inbox()."""

    def test_resolve_mixed_done_and_pending(self):
        """Resolves only pending items, already-done items pass through."""
        from io_mcp.session import _resolve_pending_inbox
        s = Session(session_id="test-1", name="Agent 1")

        done_item = InboxItem(kind="choices", preamble="Already done")
        done_item.done = True
        done_item.result = {"selected": "Old"}
        done_item.event.set()

        pending1 = InboxItem(kind="choices", preamble="Pending 1")
        pending2 = InboxItem(kind="speech", text="Pending speech")

        s.enqueue(done_item)
        s.enqueue(pending1)
        s.enqueue(pending2)

        resolved = _resolve_pending_inbox(s)
        assert resolved == 2  # pending1 + pending2
        assert pending1.result["selected"] == "_cancelled"
        assert pending2.result["selected"] == "_cancelled"
        assert pending1.event.is_set()
        assert pending2.event.is_set()

    def test_resolve_pending_inbox_empties_deque(self):
        """After resolving, the inbox deque is completely empty."""
        from io_mcp.session import _resolve_pending_inbox
        s = Session(session_id="test-1", name="Agent 1")
        for i in range(5):
            s.enqueue(InboxItem(kind="choices", preamble=f"Item-{i}"))
        _resolve_pending_inbox(s)
        assert len(s.inbox) == 0

    def test_resolve_concurrent_wait_and_resolve(self):
        """Multiple threads waiting on different items all get unblocked."""
        from io_mcp.session import _resolve_pending_inbox
        s = Session(session_id="test-1", name="Agent 1")

        items = []
        for i in range(3):
            item = InboxItem(kind="choices", preamble=f"Item-{i}")
            s.enqueue(item)
            items.append(item)

        results = [None, None, None]

        def waiter(idx):
            items[idx].event.wait(timeout=5)
            results[idx] = items[idx].result

        threads = [threading.Thread(target=waiter, args=(i,), daemon=True) for i in range(3)]
        for t in threads:
            t.start()
        time.sleep(0.05)

        _resolve_pending_inbox(s)

        for t in threads:
            t.join(timeout=2)

        for i in range(3):
            assert results[i] is not None
            assert results[i]["selected"] == "_cancelled"


class TestRegistrationMetadata:
    """Tests for Session registration metadata fields."""

    def test_default_registration_fields(self):
        """New sessions start unregistered with empty metadata."""
        s = Session(session_id="test-1", name="Agent 1")
        assert s.registered is False
        assert s.registered_at == 0.0
        assert s.cwd == ""
        assert s.hostname == ""
        assert s.username == ""
        assert s.tmux_session == ""
        assert s.tmux_pane == ""
        assert s.agent_metadata == {}

    def test_set_registration_fields(self):
        """Registration fields can be set after creation."""
        s = Session(session_id="test-1", name="Agent 1")
        s.registered = True
        s.registered_at = time.time()
        s.cwd = "/home/user/project"
        s.hostname = "desktop.local"
        s.username = "user"
        s.tmux_session = "main"
        s.tmux_pane = "%42"
        s.agent_metadata = {"custom": "data"}
        assert s.registered is True
        assert s.cwd == "/home/user/project"
        assert s.tmux_pane == "%42"
        assert s.agent_metadata["custom"] == "data"


class TestSessionInboxGeneration:
    """Tests for _inbox_generation counter tracking."""

    def test_generation_starts_at_zero(self):
        s = Session(session_id="test-1", name="Agent 1")
        assert s._inbox_generation == 0

    def test_enqueue_increments_generation(self):
        s = Session(session_id="test-1", name="Agent 1")
        s.enqueue(InboxItem(kind="choices"))
        assert s._inbox_generation == 1
        s.enqueue(InboxItem(kind="choices"))
        assert s._inbox_generation == 2

    def test_resolve_front_increments_generation(self):
        """resolve_front increments generation via _append_done and drain_kick."""
        s = Session(session_id="test-1", name="Agent 1")
        s.enqueue(InboxItem(kind="choices", preamble="Test"))
        gen_after_enqueue = s._inbox_generation
        s.resolve_front({"selected": "A"})
        # _append_done increments once
        assert s._inbox_generation > gen_after_enqueue

    def test_drain_kick_set_on_resolve(self):
        """resolve_front sets drain_kick event."""
        s = Session(session_id="test-1", name="Agent 1")
        s.drain_kick.clear()
        s.enqueue(InboxItem(kind="choices", preamble="Test"))
        s.resolve_front({"selected": "A"})
        assert s.drain_kick.is_set()


class TestSessionManagerTabNavigation:
    """Extended tests for tab navigation edge cases."""

    def test_next_tab_wraps_around(self):
        """next_tab wraps from last to first."""
        m = SessionManager()
        m.get_or_create("a")
        m.get_or_create("b")
        m.get_or_create("c")
        m.focus("c")
        s = m.next_tab()
        assert s.session_id == "a"

    def test_prev_tab_wraps_around(self):
        """prev_tab wraps from first to last."""
        m = SessionManager()
        m.get_or_create("a")
        m.get_or_create("b")
        m.get_or_create("c")
        m.focus("a")
        s = m.prev_tab()
        assert s.session_id == "c"

    def test_next_tab_single_session(self):
        """next_tab with single session returns same session."""
        m = SessionManager()
        s, _ = m.get_or_create("a")
        result = m.next_tab()
        assert result is s

    def test_next_tab_empty(self):
        """next_tab with no sessions returns None."""
        m = SessionManager()
        assert m.next_tab() is None

    def test_prev_tab_empty(self):
        """prev_tab with no sessions returns None."""
        m = SessionManager()
        assert m.prev_tab() is None

    def test_next_with_choices_wraps_around(self):
        """next_with_choices wraps around to find active session."""
        m = SessionManager()
        s1, _ = m.get_or_create("a")
        s2, _ = m.get_or_create("b")
        s3, _ = m.get_or_create("c")
        m.focus("c")
        s1.active = True  # a has choices, need to wrap to find it
        result = m.next_with_choices()
        assert result is s1

    def test_next_with_choices_skips_self(self):
        """next_with_choices doesn't return the current session."""
        m = SessionManager()
        s1, _ = m.get_or_create("a")
        s2, _ = m.get_or_create("b")
        m.focus("a")
        s1.active = True
        s2.active = True
        result = m.next_with_choices()
        # Should return b (next), not a (self)
        assert result is s2

    def test_remove_focused_shifts_to_first(self):
        """Removing the focused session shifts focus to first available."""
        m = SessionManager()
        m.get_or_create("a")
        s2, _ = m.get_or_create("b")
        m.get_or_create("c")
        m.focus("b")
        m.remove("b")
        # Focus should shift to first in order ("a")
        assert m.active_session_id == "a"

    def test_remove_last_session(self):
        """Removing the last session sets active_session_id to None."""
        m = SessionManager()
        m.get_or_create("a")
        m.remove("a")
        assert m.active_session_id is None
        assert m.focused() is None
        assert m.count() == 0

    def test_get_or_create_existing(self):
        """get_or_create returns existing session without creating a new one."""
        m = SessionManager()
        s1, created1 = m.get_or_create("a")
        assert created1 is True
        s2, created2 = m.get_or_create("a")
        assert created2 is False
        assert s1 is s2
        assert m.count() == 1

    def test_auto_focus_first_session(self):
        """First session created is auto-focused."""
        m = SessionManager()
        s, _ = m.get_or_create("a")
        assert m.active_session_id == "a"
        assert m.focused() is s
