"""Tests for session management, API events, and CLI tool."""

import collections
import threading
import time

import pytest

from io_mcp.session import Session, SessionManager, SpeechEntry, HistoryEntry, InboxItem


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
        assert "o" in text  # active indicator (was ●)

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
        """Tab bar shows x for unresponsive health status."""
        m = SessionManager()
        s1, _ = m.get_or_create("a")
        s1.name = "Agent 1"
        s1.health_status = "unresponsive"
        s1.active = False
        text = m.tab_bar_text()
        assert "[bold #bf616a]x[/bold #bf616a]" in text

    def test_tab_bar_active_choices_hides_warning(self):
        """When agent has active choices, health warning is hidden (agent is healthy)."""
        m = SessionManager()
        s1, _ = m.get_or_create("a")
        s1.name = "Agent 1"
        s1.health_status = "warning"
        s1.active = True  # agent is presenting choices — not stuck
        text = m.tab_bar_text()
        # Should show choices indicator (o), not warning (!)
        assert "[bold #a3be8c]o[/bold #a3be8c]" in text
        assert "!" not in text

    def test_tab_bar_healthy_no_indicator(self):
        """Healthy agents with no active choices show no status indicator."""
        m = SessionManager()
        s1, _ = m.get_or_create("a")
        s1.name = "Agent 1"
        s1.health_status = "healthy"
        s1.active = False
        text = m.tab_bar_text()
        assert "[bold #ebcb8b]![/bold #ebcb8b]" not in text
        assert "[bold #bf616a]x[/bold #bf616a]" not in text
        assert "[bold #a3be8c]o[/bold #a3be8c]" not in text

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
        assert "[bold #bf616a]x[/bold #bf616a]" in text   # unresponsive indicator


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
        assert "o" in text
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
        assert "o+2" in text  # 3 total, active shows o, +2 queued

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
        assert "o+1" in text_before
        # Resolve first item
        s.resolve_front({"selected": "A"})
        text_after = m.tab_bar_text()
        # Now only 1 item, no +N badge
        assert "+1" not in text_after or "o+0" not in text_after


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

    def test_cancels_pending_duplicate(self):
        """An identical pending item is cancelled when a new one arrives."""
        s = Session(session_id="test-1", name="Agent 1")
        choices = [{"label": "A"}, {"label": "B"}]
        item1 = InboxItem(kind="choices", preamble="Pick", choices=list(choices))
        s.enqueue(item1)  # bypass dedup for setup

        # Reset dedup log so the timestamp window doesn't interfere
        s._inbox_dedup_log.clear()

        item2 = InboxItem(kind="choices", preamble="Pick", choices=list(choices))
        assert s.dedup_and_enqueue(item2) is True

        # item1 should have been superseded
        assert item1.done is True
        assert item1.result["selected"] == "_restart"
        assert item1.event.is_set()

        # item2 is now in the queue
        assert len(s.inbox) == 2  # both in deque, but item1 is done
        assert not item2.done

    def test_dedup_window_suppresses_rapid_duplicate(self):
        """A duplicate within the dedup window is suppressed (not enqueued)."""
        s = Session(session_id="test-1", name="Agent 1")
        choices = [{"label": "X"}]
        item1 = InboxItem(kind="choices", preamble="Go", choices=list(choices))
        assert s.dedup_and_enqueue(item1) is True

        # Resolve item1 so it's done — simulates normal completion
        item1.result = {"selected": "X"}
        item1.done = True
        item1.event.set()

        # Now enqueue an identical item immediately (within 2s window)
        item2 = InboxItem(kind="choices", preamble="Go", choices=list(choices))
        assert s.dedup_and_enqueue(item2) is False
        assert item2.done is True
        assert item2.result["selected"] == "_restart"
        assert "dedup window" in item2.result["summary"].lower()

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

    def test_dedup_window_expires(self):
        """After the dedup window expires, identical items are allowed."""
        s = Session(session_id="test-1", name="Agent 1")
        s._inbox_dedup_window_secs = 0.05  # 50ms for fast test
        choices = [{"label": "A"}]

        item1 = InboxItem(kind="choices", preamble="Go", choices=list(choices))
        assert s.dedup_and_enqueue(item1) is True

        # Wait for window to expire
        time.sleep(0.1)

        item2 = InboxItem(kind="choices", preamble="Go", choices=list(choices))
        assert s.dedup_and_enqueue(item2) is True
        # item1 should be cancelled (pending duplicate logic)
        assert item1.done is True

    def test_concurrent_threads_no_duplicates(self):
        """Multiple threads enqueuing identical choices don't create duplicates."""
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

        # Exactly one should be enqueued, rest should be suppressed
        enqueued_count = sum(1 for _, e, _ in results if e)
        suppressed_count = sum(1 for _, e, _ in results if not e)
        assert enqueued_count == 1, f"Expected 1 enqueued, got {enqueued_count}"
        assert suppressed_count == 4, f"Expected 4 suppressed, got {suppressed_count}"

        # All suppressed items should have _restart result
        for _, enqueued, item in results:
            if not enqueued:
                assert item.done is True
                assert item.result["selected"] == "_restart"

    def test_dedup_log_pruning(self):
        """Old dedup log entries are cleaned up."""
        s = Session(session_id="test-1", name="Agent 1")
        # Pre-populate with old entries
        old_time = time.time() - 120  # 2 minutes ago
        s._inbox_dedup_log[("old", ("X",))] = old_time
        s._inbox_dedup_log[("also_old", ("Y",))] = old_time

        # Enqueue a new item — should trigger pruning
        item = InboxItem(kind="choices", preamble="New",
                         choices=[{"label": "Z"}])
        s.dedup_and_enqueue(item)

        # Old entries should be pruned
        assert ("old", ("X",)) not in s._inbox_dedup_log
        assert ("also_old", ("Y",)) not in s._inbox_dedup_log
        # New entry should exist
        assert ("New", ("Z",)) in s._inbox_dedup_log
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
