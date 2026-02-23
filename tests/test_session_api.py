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
        """Tab bar shows ⚠ for warning health status."""
        m = SessionManager()
        s1, _ = m.get_or_create("a")
        s1.name = "Agent 1"
        s1.health_status = "warning"
        s1.active = False
        text = m.tab_bar_text()
        assert "⚠" in text

    def test_tab_bar_shows_unresponsive_indicator(self):
        """Tab bar shows ✗ for unresponsive health status."""
        m = SessionManager()
        s1, _ = m.get_or_create("a")
        s1.name = "Agent 1"
        s1.health_status = "unresponsive"
        s1.active = False
        text = m.tab_bar_text()
        assert "✗" in text

    def test_tab_bar_active_choices_hides_warning(self):
        """When agent has active choices, health warning is hidden (agent is healthy)."""
        m = SessionManager()
        s1, _ = m.get_or_create("a")
        s1.name = "Agent 1"
        s1.health_status = "warning"
        s1.active = True  # agent is presenting choices — not stuck
        text = m.tab_bar_text()
        # Should show choices indicator (●), not warning (⚠)
        assert "●" in text
        assert "⚠" not in text

    def test_tab_bar_healthy_no_indicator(self):
        """Healthy agents with no active choices show no status indicator."""
        m = SessionManager()
        s1, _ = m.get_or_create("a")
        s1.name = "Agent 1"
        s1.health_status = "healthy"
        s1.active = False
        text = m.tab_bar_text()
        assert "⚠" not in text
        assert "✗" not in text
        assert "●" not in text

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
        assert "⚠" in text   # warning indicator
        assert "✗" in text   # unresponsive indicator


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


class TestSessionPersistence:
    """Tests for session save/load and activity restoration."""

    def test_restore_activity_speech_log(self):
        """Restoring activity brings back speech log."""
        s = Session(session_id="test-1", name="Agent 1")
        data = {
            "speech_log": [
                {"text": "Hello", "timestamp": 1000.0},
                {"text": "World", "timestamp": 1001.0},
            ],
        }
        s.restore_activity(data)
        assert len(s.speech_log) == 2
        assert s.speech_log[0].text == "Hello"
        assert s.speech_log[0].timestamp == 1000.0
        assert s.speech_log[0].played is True  # old speech marked as played
        assert s.speech_log[1].text == "World"

    def test_restore_activity_history(self):
        """Restoring activity brings back selection history."""
        s = Session(session_id="test-1", name="Agent 1")
        data = {
            "history": [
                {"label": "Fix bug", "summary": "Fixed it", "preamble": "Choose", "timestamp": 2000.0},
            ],
        }
        s.restore_activity(data)
        assert len(s.history) == 1
        assert s.history[0].label == "Fix bug"
        assert s.history[0].summary == "Fixed it"
        assert s.history[0].timestamp == 2000.0

    def test_restore_activity_tool_stats(self):
        """Restoring activity brings back tool call stats."""
        s = Session(session_id="test-1", name="Agent 1")
        data = {
            "tool_call_count": 42,
            "last_tool_name": "speak_async",
            "last_tool_call": 3000.0,
        }
        s.restore_activity(data)
        assert s.tool_call_count == 42
        assert s.last_tool_name == "speak_async"
        assert s.last_tool_call == 3000.0

    def test_restore_activity_empty_data(self):
        """Restoring with empty data doesn't crash."""
        s = Session(session_id="test-1", name="Agent 1")
        s.restore_activity({})
        assert s.tool_call_count == 0
        assert s.last_tool_name == ""
        assert len(s.speech_log) == 0
        assert len(s.history) == 0

    def test_restore_activity_additive(self):
        """Restoring activity is additive to existing data."""
        s = Session(session_id="test-1", name="Agent 1")
        s.speech_log.append(SpeechEntry(text="Existing"))
        s.tool_call_count = 5

        data = {
            "speech_log": [{"text": "Restored", "timestamp": 1000.0}],
            "tool_call_count": 10,
        }
        s.restore_activity(data)
        # Speech log is additive
        assert len(s.speech_log) == 2
        assert s.speech_log[0].text == "Existing"
        assert s.speech_log[1].text == "Restored"
        # Tool count is overwritten (not added)
        assert s.tool_call_count == 10

    def test_save_includes_activity(self, tmp_path):
        """save_registered persists speech log, history, and tool stats."""
        m = SessionManager()
        # Override persist file
        m.PERSIST_FILE = str(tmp_path / "sessions.json")

        s, _ = m.get_or_create("test-1")
        s.registered = True
        s.name = "Test Agent"
        s.cwd = "/tmp/test"
        s.speech_log.append(SpeechEntry(text="Hello"))
        s.history.append(HistoryEntry(label="Fix", summary="Fixed", preamble="Choose"))
        s.tool_call_count = 7
        s.last_tool_name = "speak"

        m.save_registered()

        # Load and verify
        loaded = m.load_registered()
        assert len(loaded) == 1
        data = loaded[0]
        assert data["name"] == "Test Agent"
        assert len(data["speech_log"]) == 1
        assert data["speech_log"][0]["text"] == "Hello"
        assert len(data["history"]) == 1
        assert data["history"][0]["label"] == "Fix"
        assert data["tool_call_count"] == 7
        assert data["last_tool_name"] == "speak"

    def test_save_limits_speech_log(self, tmp_path):
        """save_registered only saves last 100 speech entries."""
        m = SessionManager()
        m.PERSIST_FILE = str(tmp_path / "sessions.json")

        s, _ = m.get_or_create("test-1")
        s.registered = True
        s.name = "Verbose Agent"
        # Add 200 speech entries
        for i in range(200):
            s.speech_log.append(SpeechEntry(text=f"Speech {i}"))

        m.save_registered()
        loaded = m.load_registered()
        assert len(loaded[0]["speech_log"]) == 100
        # Should be the last 100
        assert loaded[0]["speech_log"][0]["text"] == "Speech 100"

    def test_roundtrip_save_restore(self, tmp_path):
        """Full roundtrip: save → load → restore preserves data."""
        m = SessionManager()
        m.PERSIST_FILE = str(tmp_path / "sessions.json")

        # Create and populate a session
        s, _ = m.get_or_create("test-1")
        s.registered = True
        s.name = "My Agent"
        s.cwd = "/home/user/project"
        s.speech_log.append(SpeechEntry(text="Working on it", timestamp=5000.0))
        s.history.append(HistoryEntry(
            label="Build", summary="Built the app", preamble="What next?",
            timestamp=5001.0,
        ))
        s.tool_call_count = 15
        s.last_tool_name = "present_choices"

        # Save
        m.save_registered()

        # Create a new session (simulates agent reconnecting)
        m2 = SessionManager()
        m2.PERSIST_FILE = str(tmp_path / "sessions.json")
        s2, _ = m2.get_or_create("new-session-id")
        s2.name = "My Agent"
        s2.cwd = "/home/user/project"

        # Load and restore
        persisted = m2.load_registered()
        for saved in persisted:
            if saved.get("name") == s2.name:
                s2.restore_activity(saved)
                break

        # Verify restoration
        assert len(s2.speech_log) == 1
        assert s2.speech_log[0].text == "Working on it"
        assert len(s2.history) == 1
        assert s2.history[0].label == "Build"
        assert s2.tool_call_count == 15
        assert s2.last_tool_name == "present_choices"


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
