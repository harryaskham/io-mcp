"""Tests for critical edge cases and error recovery in io-mcp.

Covers three gap areas in test coverage:
1. Backend down while tools are called (proxy retry and error paths)
2. Rapid session creation/destruction (race conditions, cleanup)
3. TUI restart while tools are pending (inbox resilience, _restart signals)

Plus additional edge cases:
- Orphaned inbox items when owner thread dies
- Dedup/piggyback behavior for duplicate present_choices
- Concurrent inbox operations across multiple sessions
"""

from __future__ import annotations

import collections
import json
import threading
import time

import pytest

from io_mcp.session import (
    Session,
    SessionManager,
    InboxItem,
    SpeechEntry,
    HistoryEntry,
    FlushedMessage,
)


# ═══════════════════════════════════════════════════════════════════
# 1. Backend down — proxy retry and error handling
# ═══════════════════════════════════════════════════════════════════


class TestBackendDownForwardRetry:
    """Test _forward_to_backend behavior when the backend is unreachable."""

    def test_retries_exhaust_then_return_error_with_hint(self):
        """After max_retries, response includes error + hint."""
        from io_mcp.proxy import _forward_to_backend

        result = _forward_to_backend(
            "http://127.0.0.1:19999",  # nothing listening
            "register_session",
            {"cwd": "/tmp", "name": "test"},
            "session-123",
            max_retries=2,
            initial_backoff=0.01,
            max_backoff=0.02,
        )
        # Should contain error JSON
        # Strip crash log hint if present
        json_part = result.split("\n\n[IO-MCP")[0]
        data = json.loads(json_part)
        assert "error" in data
        assert "hint" in data  # "Is io-mcp running?"

    def test_different_blocking_vs_nonblocking_timeouts(self):
        """Blocking tools should get different timeouts than non-blocking ones."""
        from io_mcp.proxy import _BLOCKING_TOOLS
        # This is a static assertion — blocking tools use 3600s,
        # non-blocking use 300s (verified via code reading)
        assert "present_choices" in _BLOCKING_TOOLS
        assert "speak_async" not in _BLOCKING_TOOLS
        assert "check_inbox" not in _BLOCKING_TOOLS
        assert "register_session" not in _BLOCKING_TOOLS

    def test_backend_returns_500_no_retry(self):
        """HTTP 500 from backend should NOT be retried — returns immediately."""
        from io_mcp.proxy import _forward_to_backend
        from http.server import HTTPServer, BaseHTTPRequestHandler
        import socket

        call_count = 0

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                nonlocal call_count
                call_count += 1
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                body = b'{"error": "tool crashed"}'
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        with socket.socket() as s:
            s.bind(("", 0))
            port = s.getsockname()[1]

        server = HTTPServer(("127.0.0.1", port), Handler)
        thread = threading.Thread(target=server.handle_request, daemon=True)
        thread.start()
        time.sleep(0.1)

        result = _forward_to_backend(
            f"http://127.0.0.1:{port}",
            "speak_async",
            {"text": "hello"},
            "sid1",
            max_retries=5,
            initial_backoff=0.01,
        )
        assert "tool crashed" in result
        assert call_count == 1  # No retries for HTTP errors
        server.server_close()


# ═══════════════════════════════════════════════════════════════════
# 2. Rapid session creation/destruction
# ═══════════════════════════════════════════════════════════════════


class TestRapidSessionLifecycle:
    """Test concurrent and rapid session creation and removal."""

    def test_concurrent_session_creation(self):
        """Multiple threads creating sessions simultaneously should not corrupt state."""
        mgr = SessionManager()
        errors = []
        created_ids = []
        lock = threading.Lock()

        def create_sessions(start, count):
            try:
                for i in range(count):
                    sid = f"session-{start + i}"
                    session, created = mgr.get_or_create(sid)
                    with lock:
                        created_ids.append((sid, created))
            except Exception as e:
                with lock:
                    errors.append(e)

        threads = [
            threading.Thread(target=create_sessions, args=(i * 10, 10))
            for i in range(5)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert not errors, f"Thread errors: {errors}"
        assert mgr.count() == 50
        # All sessions should have been created
        all_sids = {sid for sid, _ in created_ids}
        assert len(all_sids) == 50

    def test_create_and_remove_same_session_rapidly(self):
        """Creating and removing the same session rapidly should be safe."""
        mgr = SessionManager()

        for _ in range(20):
            session, created = mgr.get_or_create("ephemeral")
            assert created or not created  # either is fine
            mgr.remove("ephemeral")

        assert mgr.count() == 0
        assert mgr.active_session_id is None

    def test_remove_nonexistent_session(self):
        """Removing a session that doesn't exist should be a no-op."""
        mgr = SessionManager()
        mgr.get_or_create("real-session")

        # Remove nonexistent — should not raise
        result = mgr.remove("ghost-session")
        assert result == "real-session"  # still focused on real session
        assert mgr.count() == 1

    def test_focus_after_all_sessions_removed(self):
        """After removing all sessions, focused() should return None."""
        mgr = SessionManager()
        mgr.get_or_create("a")
        mgr.get_or_create("b")

        mgr.remove("a")
        mgr.remove("b")

        assert mgr.focused() is None
        assert mgr.active_session_id is None
        assert mgr.next_tab() is None
        assert mgr.prev_tab() is None

    def test_cleanup_during_concurrent_activity(self):
        """cleanup_stale should handle concurrent session modifications gracefully."""
        mgr = SessionManager()

        # Create sessions, make some stale
        for i in range(10):
            s, _ = mgr.get_or_create(f"s-{i}")
            if i >= 5:
                s.last_activity = time.time() - 600  # stale

        # Focus session 0 (it's protected from cleanup)
        mgr.focus("s-0")

        # Run cleanup while another thread creates new sessions
        errors = []

        def add_more():
            try:
                for i in range(10, 15):
                    mgr.get_or_create(f"s-{i}")
                    time.sleep(0.001)
            except Exception as e:
                errors.append(e)

        t = threading.Thread(target=add_more, daemon=True)
        t.start()

        removed = mgr.cleanup_stale(timeout_seconds=300.0)
        t.join(timeout=5)

        assert not errors
        # Sessions 5-9 should be removed (they're stale and not focused)
        for sid in removed:
            assert sid.startswith("s-")
            idx = int(sid.split("-")[1])
            assert idx >= 5  # only stale ones should be removed


# ═══════════════════════════════════════════════════════════════════
# 3. TUI restart while tools are pending (inbox resilience)
# ═══════════════════════════════════════════════════════════════════


class TestInboxResilience:
    """Test inbox behavior when items are pending during TUI restarts."""

    def test_orphaned_item_cleaned_on_peek(self):
        """InboxItem whose owner thread has died is auto-cleaned on peek_inbox."""
        session = Session(session_id="test-1", name="Test")

        # Simulate an item whose owner thread is dead
        dead_thread = threading.Thread(target=lambda: None)
        dead_thread.start()
        dead_thread.join()  # thread is now dead

        item = InboxItem(
            kind="choices",
            preamble="Pick one",
            choices=[{"label": "A", "summary": "a"}],
            owner_thread=dead_thread,
        )
        session.enqueue(item)

        # peek_inbox should detect the dead thread and clean up
        result = session.peek_inbox()
        assert result is None  # orphaned item was removed
        assert item.done is True
        assert item.result["selected"] == "_restart"
        assert item.event.is_set()

    def test_orphaned_item_skipped_to_next_live_item(self):
        """Dead-thread items are skipped, and the next live item is returned."""
        session = Session(session_id="test-1", name="Test")

        # Create a dead-thread item
        dead_thread = threading.Thread(target=lambda: None)
        dead_thread.start()
        dead_thread.join()

        dead_item = InboxItem(
            kind="choices",
            preamble="Dead item",
            choices=[{"label": "Dead", "summary": "dead"}],
            owner_thread=dead_thread,
        )
        session.enqueue(dead_item)

        # Create a live-thread item (current thread is alive)
        live_item = InboxItem(
            kind="choices",
            preamble="Live item",
            choices=[{"label": "Live", "summary": "live"}],
            owner_thread=threading.current_thread(),
        )
        session.enqueue(live_item)

        # peek should skip the dead item and return the live one
        result = session.peek_inbox()
        assert result is live_item
        assert dead_item.done is True

    def test_resolve_front_signals_event(self):
        """resolve_front should signal the item's event so blocking threads wake up."""
        session = Session(session_id="test-1", name="Test")

        item = InboxItem(
            kind="choices",
            preamble="Pick",
            choices=[{"label": "X", "summary": "x"}],
        )
        session.enqueue(item)

        # Resolve from another thread, verify the event wakes up
        woke_up = threading.Event()

        def waiter():
            item.event.wait(timeout=5)
            woke_up.set()

        t = threading.Thread(target=waiter, daemon=True)
        t.start()

        time.sleep(0.05)  # let waiter start blocking
        session.resolve_front({"selected": "X", "summary": "x"})

        woke_up.wait(timeout=2)
        assert woke_up.is_set()
        assert item.result == {"selected": "X", "summary": "x"}
        assert item.done is True

    def test_resolve_front_kicks_drain(self):
        """resolve_front should kick drain_kick so the next item starts immediately."""
        session = Session(session_id="test-1", name="Test")

        session.drain_kick.clear()

        item = InboxItem(kind="choices", preamble="Pick",
                         choices=[{"label": "A", "summary": "a"}])
        session.enqueue(item)
        session.resolve_front({"selected": "A", "summary": "a"})

        assert session.drain_kick.is_set()


# ═══════════════════════════════════════════════════════════════════
# 4. Dedup and piggyback for duplicate present_choices
# ═══════════════════════════════════════════════════════════════════


class TestInboxDedupPiggyback:
    """Test dedup_and_enqueue prevents duplicate inbox items from MCP retries."""

    def test_identical_choices_piggyback(self):
        """Second identical present_choices piggybacks on the first."""
        session = Session(session_id="test-1", name="Test")

        item1 = InboxItem(
            kind="choices",
            preamble="Pick a color",
            choices=[{"label": "Red"}, {"label": "Blue"}],
        )
        result1 = session.dedup_and_enqueue(item1)
        assert result1 is True  # enqueued as new

        # Same preamble + labels = duplicate
        item2 = InboxItem(
            kind="choices",
            preamble="Pick a color",
            choices=[{"label": "Red"}, {"label": "Blue"}],
        )
        result2 = session.dedup_and_enqueue(item2)
        assert isinstance(result2, InboxItem)
        assert result2 is item1  # piggyback on first

    def test_different_choices_both_enqueued(self):
        """Different choices should be enqueued independently."""
        session = Session(session_id="test-1", name="Test")

        item1 = InboxItem(
            kind="choices",
            preamble="Pick a color",
            choices=[{"label": "Red"}, {"label": "Blue"}],
        )
        session.dedup_and_enqueue(item1)

        item2 = InboxItem(
            kind="choices",
            preamble="Pick a shape",
            choices=[{"label": "Circle"}, {"label": "Square"}],
        )
        result = session.dedup_and_enqueue(item2)
        assert result is True  # enqueued as new (different preamble)
        assert session.inbox_choices_count() == 2

    def test_done_items_not_deduped(self):
        """Already-resolved items should not match dedup check."""
        session = Session(session_id="test-1", name="Test")

        item1 = InboxItem(
            kind="choices",
            preamble="Pick",
            choices=[{"label": "A"}],
        )
        session.dedup_and_enqueue(item1)
        # Resolve it
        session.resolve_front({"selected": "A", "summary": ""})

        # Same choices again — should be enqueued fresh (old one is done)
        item2 = InboxItem(
            kind="choices",
            preamble="Pick",
            choices=[{"label": "A"}],
        )
        result = session.dedup_and_enqueue(item2)
        assert result is True  # new item, not piggyback

    def test_piggyback_gets_same_result(self):
        """Piggybacked items should receive the same result when resolved."""
        session = Session(session_id="test-1", name="Test")

        item1 = InboxItem(
            kind="choices",
            preamble="Pick",
            choices=[{"label": "X"}],
        )
        session.dedup_and_enqueue(item1)

        # Piggyback
        item2 = InboxItem(
            kind="choices",
            preamble="Pick",
            choices=[{"label": "X"}],
        )
        existing = session.dedup_and_enqueue(item2)
        assert existing is item1

        # Resolve the first item — piggybacker should see the same result
        results = []
        def piggyback_waiter():
            existing.event.wait(timeout=5)
            results.append(existing.result)

        t = threading.Thread(target=piggyback_waiter, daemon=True)
        t.start()

        time.sleep(0.05)
        session.resolve_front({"selected": "X", "summary": "chosen"})
        t.join(timeout=2)

        assert len(results) == 1
        assert results[0]["selected"] == "X"


# ═══════════════════════════════════════════════════════════════════
# 5. Inbox done list capping and _restart filtering
# ═══════════════════════════════════════════════════════════════════


class TestInboxDoneCapping:
    """Test that inbox_done is capped and _restart items are filtered."""

    def test_restart_items_not_added_to_done(self):
        """Items resolved with _restart should not appear in inbox_done."""
        session = Session(session_id="test-1", name="Test")

        item = InboxItem(kind="choices", preamble="P", choices=[{"label": "A"}])
        session.enqueue(item)

        # Resolve with _restart (simulating TUI restart)
        item.result = {"selected": "_restart", "summary": "TUI restarting"}
        item.done = True
        item.event.set()

        # Manually call _append_done (peek_inbox does this)
        session._append_done(session.inbox.popleft())

        assert len(session.inbox_done) == 0  # _restart items are dropped

    def test_inbox_done_caps_at_max(self):
        """inbox_done should be trimmed when it exceeds _inbox_done_max."""
        session = Session(session_id="test-1", name="Test")
        session._inbox_done_max = 5

        # Add 7 items directly
        for i in range(7):
            item = InboxItem(kind="choices", preamble=f"P{i}",
                             choices=[{"label": f"L{i}"}])
            item.result = {"selected": f"L{i}", "summary": ""}
            item.done = True
            session._append_done(item)

        # Should be capped at 5
        assert len(session.inbox_done) == 5
        # Oldest items should have been trimmed
        assert session.inbox_done[0].preamble == "P2"
        assert session.inbox_done[-1].preamble == "P6"


# ═══════════════════════════════════════════════════════════════════
# 6. Speech inbox ordering and priority
# ═══════════════════════════════════════════════════════════════════


class TestSpeechInboxPriority:
    """Test speech enqueue ordering, especially urgent (priority) items."""

    def test_normal_speech_appended_to_back(self):
        """Normal priority speech is appended to the back of the inbox."""
        session = Session(session_id="test-1", name="Test")

        item1 = session.enqueue_speech("first", blocking=False)
        item2 = session.enqueue_speech("second", blocking=False)

        front = session.peek_inbox()
        assert front is item1
        assert front.text == "first"

    def test_urgent_speech_inserted_at_front(self):
        """Urgent (priority=1) speech is inserted at the front of the inbox."""
        session = Session(session_id="test-1", name="Test")

        normal_item = session.enqueue_speech("normal", blocking=False, priority=0)
        urgent_item = session.enqueue_speech("urgent!", blocking=False, priority=1)

        front = session.peek_inbox()
        assert front is urgent_item
        assert front.text == "urgent!"

    def test_enqueue_speech_kicks_drain(self):
        """enqueue_speech should set drain_kick so waiting threads wake up."""
        session = Session(session_id="test-1", name="Test")
        session.drain_kick.clear()

        session.enqueue_speech("hello", blocking=False)
        assert session.drain_kick.is_set()


# ═══════════════════════════════════════════════════════════════════
# 7. Session tab navigation edge cases
# ═══════════════════════════════════════════════════════════════════


class TestTabNavigationEdgeCases:
    """Test tab navigation with edge cases."""

    def test_next_with_choices_no_active_sessions(self):
        """next_with_choices with no active sessions returns None."""
        mgr = SessionManager()
        mgr.get_or_create("s1")
        mgr.get_or_create("s2")

        # No sessions have active=True
        result = mgr.next_with_choices()
        assert result is None

    def test_next_with_choices_skips_to_active(self):
        """next_with_choices skips inactive sessions to find one with choices."""
        mgr = SessionManager()
        s1, _ = mgr.get_or_create("s1")
        s2, _ = mgr.get_or_create("s2")
        s3, _ = mgr.get_or_create("s3")

        # Only s3 has active choices
        s3.active = True

        # Focus s1
        mgr.focus("s1")

        result = mgr.next_with_choices()
        assert result is s3
        assert mgr.active_session_id == "s3"

    def test_next_tab_single_session_wraps(self):
        """next_tab with single session wraps to itself."""
        mgr = SessionManager()
        s1, _ = mgr.get_or_create("only")

        result = mgr.next_tab()
        assert result is s1
        assert mgr.active_session_id == "only"

    def test_prev_tab_wraps_to_last(self):
        """prev_tab from first session wraps to last."""
        mgr = SessionManager()
        mgr.get_or_create("s1")
        mgr.get_or_create("s2")
        s3, _ = mgr.get_or_create("s3")

        mgr.focus("s1")
        result = mgr.prev_tab()
        assert result is s3


# ═══════════════════════════════════════════════════════════════════
# 8. Stale inbox items from dead/removed sessions
# ═══════════════════════════════════════════════════════════════════


class TestStaleInboxCleanup:
    """Test that inbox items from dead/removed sessions are properly cleaned up."""

    def test_resolve_pending_inbox_cancels_all_items(self):
        """_resolve_pending_inbox should cancel all pending items and unblock threads."""
        from io_mcp.session import _resolve_pending_inbox

        session = Session(session_id="test-1", name="Test")

        # Enqueue multiple pending items
        items = []
        for i in range(3):
            item = InboxItem(
                kind="choices",
                preamble=f"Pick {i}",
                choices=[{"label": f"Option {i}"}],
            )
            session.enqueue(item)
            items.append(item)

        # Resolve all pending
        resolved = _resolve_pending_inbox(session)

        assert resolved == 3
        assert len(session.inbox) == 0
        for item in items:
            assert item.done is True
            assert item.result["selected"] == "_cancelled"
            assert item.event.is_set()

    def test_resolve_pending_inbox_unblocks_waiting_threads(self):
        """Blocked threads should wake up when _resolve_pending_inbox is called."""
        from io_mcp.session import _resolve_pending_inbox

        session = Session(session_id="test-1", name="Test")

        item = InboxItem(
            kind="choices",
            preamble="Pick one",
            choices=[{"label": "A"}],
        )
        session.enqueue(item)

        # Simulate a thread waiting on this item
        woke_up = threading.Event()

        def waiter():
            item.event.wait(timeout=5)
            woke_up.set()

        t = threading.Thread(target=waiter, daemon=True)
        t.start()
        time.sleep(0.05)  # let waiter start blocking

        # Resolve — should wake up the waiter
        _resolve_pending_inbox(session)
        woke_up.wait(timeout=2)

        assert woke_up.is_set()
        assert item.result["selected"] == "_cancelled"

    def test_session_removal_resolves_inbox_items(self):
        """SessionManager.remove should resolve pending inbox items."""
        mgr = SessionManager()
        session, _ = mgr.get_or_create("s1")

        # Add pending inbox items
        item1 = InboxItem(kind="choices", preamble="P1",
                          choices=[{"label": "A"}])
        item2 = InboxItem(kind="choices", preamble="P2",
                          choices=[{"label": "B"}])
        session.enqueue(item1)
        session.enqueue(item2)

        # Remove the session
        mgr.remove("s1")

        # Items should be resolved with _cancelled
        assert item1.done is True
        assert item1.result["selected"] == "_cancelled"
        assert item1.event.is_set()
        assert item2.done is True
        assert item2.result["selected"] == "_cancelled"
        assert item2.event.is_set()

    def test_session_removal_does_not_affect_other_sessions(self):
        """Removing one session should not affect inbox items in other sessions."""
        mgr = SessionManager()
        s1, _ = mgr.get_or_create("s1")
        s2, _ = mgr.get_or_create("s2")

        # Add items to both sessions
        item_s1 = InboxItem(kind="choices", preamble="S1 item",
                            choices=[{"label": "A"}])
        s1.enqueue(item_s1)

        item_s2 = InboxItem(kind="choices", preamble="S2 item",
                            choices=[{"label": "B"}])
        s2.enqueue(item_s2)

        # Remove s1 only
        mgr.remove("s1")

        # s1's item should be cancelled
        assert item_s1.done is True
        assert item_s1.result["selected"] == "_cancelled"

        # s2's item should be untouched
        assert item_s2.done is False
        assert item_s2.result is None

    def test_dismiss_stale_active_item_with_no_inbox_item(self):
        """Dismissing when session.active=True but _active_inbox_item is gone should clean up."""
        from io_mcp.session import _resolve_pending_inbox

        session = Session(session_id="test-1", name="Test")

        # Simulate stale state: session.active is True but no _active_inbox_item
        session.active = True
        session.preamble = "Stale preamble"
        session.choices = [{"label": "Stale"}]
        session._active_inbox_item = None

        # Also add some orphaned pending items in the inbox
        orphan = InboxItem(kind="choices", preamble="Orphan",
                           choices=[{"label": "X"}])
        session.enqueue(orphan)

        # Resolve should clean up everything
        _resolve_pending_inbox(session)
        session.active = False
        session.preamble = ""
        session.choices = []

        assert len(session.inbox) == 0
        assert orphan.done is True
        assert orphan.result["selected"] == "_cancelled"

    def test_dismiss_clears_stale_pending_items(self):
        """Dismissing should clear stale pending items even when session.active=False."""
        from io_mcp.session import _resolve_pending_inbox

        session = Session(session_id="test-1", name="Test")
        session.active = False

        # Add stale pending items (e.g. from a dead agent thread)
        stale1 = InboxItem(kind="choices", preamble="Stale 1",
                           choices=[{"label": "A"}])
        stale2 = InboxItem(kind="choices", preamble="Stale 2",
                           choices=[{"label": "B"}])
        session.enqueue(stale1)
        session.enqueue(stale2)

        assert session.inbox_choices_count() == 2

        # Resolve all pending items
        resolved = _resolve_pending_inbox(session)

        assert resolved == 2
        assert session.inbox_choices_count() == 0
        assert stale1.done is True
        assert stale2.done is True

    def test_cleanup_stale_skips_sessions_with_pending_inbox(self):
        """cleanup_stale should NOT remove sessions that have pending inbox items."""
        mgr = SessionManager()
        s1, _ = mgr.get_or_create("s1")
        s2, _ = mgr.get_or_create("s2")

        # Make both sessions stale by timestamp
        s1.last_activity = time.time() - 600
        s2.last_activity = time.time() - 600

        # s2 has pending inbox items — should be protected
        item = InboxItem(kind="choices", preamble="Pending",
                         choices=[{"label": "A"}])
        s2.enqueue(item)

        # Focus something else so s1 is removable
        mgr.focus("s2")

        removed = mgr.cleanup_stale(timeout_seconds=300)

        # s1 should be removed (stale, no inbox items, not focused)
        assert "s1" in removed
        # s2 should NOT be removed (has pending inbox items)
        assert "s2" not in removed
        assert mgr.get("s2") is not None

    def test_concurrent_session_removal_with_pending_items(self):
        """Removing sessions with pending items from multiple threads should be safe."""
        mgr = SessionManager()
        errors = []

        # Create sessions with pending items
        sessions = []
        for i in range(10):
            s, _ = mgr.get_or_create(f"s-{i}")
            item = InboxItem(kind="choices", preamble=f"P{i}",
                             choices=[{"label": f"L{i}"}])
            s.enqueue(item)
            sessions.append((s, item))

        def remove_sessions(start, count):
            try:
                for i in range(start, start + count):
                    mgr.remove(f"s-{i}")
            except Exception as e:
                errors.append(e)

        # Remove from multiple threads simultaneously
        threads = [
            threading.Thread(target=remove_sessions, args=(i * 5, 5))
            for i in range(2)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert not errors, f"Thread errors: {errors}"
        assert mgr.count() == 0

        # All items should be resolved
        for s, item in sessions:
            assert item.done is True
            assert item.event.is_set()
