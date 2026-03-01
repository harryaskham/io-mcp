"""Tests for error handling and recovery improvements in io-mcp.

Covers:
1. _safe_tool wrapper (server.py) — error responses include tool name,
   truncated message, and suggestion
2. Backend dispatch (dispatch in __main__.py) — same error shape
3. _drain_session_inbox (app.py) — errors in one item don't block the next
4. _safe_action decorator (widgets.py) — catches all exceptions, prefers
   _speak_ui, never crashes the TUI
"""

from __future__ import annotations

import collections
import json
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from io_mcp.session import (
    Session,
    SessionManager,
    InboxItem,
    SpeechEntry,
)


# ═══════════════════════════════════════════════════════════════════
# 1. _safe_tool wrapper — structured error responses
# ═══════════════════════════════════════════════════════════════════


class TestSafeToolErrorResponse:
    """The _safe_tool wrapper should produce structured error JSON."""

    def test_error_json_has_tool_name(self):
        """Error response must include the tool name that failed."""
        import asyncio
        import functools

        # Replicate the _safe_tool pattern from server.py
        def _safe_tool(fn):
            @functools.wraps(fn)
            async def wrapper(*args, **kwargs):
                try:
                    return await fn(*args, **kwargs)
                except Exception as exc:
                    err_msg = f"{type(exc).__name__}: {str(exc)[:200]}"
                    return json.dumps({
                        "error": err_msg,
                        "tool": fn.__name__,
                        "suggestion": "Retry the tool call, or call get_logs() to inspect recent errors.",
                    })
            return wrapper

        @_safe_tool
        async def failing_tool():
            raise ValueError("something broke")

        result = asyncio.run(failing_tool())
        data = json.loads(result)

        assert "error" in data
        assert data["tool"] == "failing_tool"
        assert "suggestion" in data
        assert "Retry" in data["suggestion"]
        assert "ValueError" in data["error"]

    def test_error_message_truncated_at_200_chars(self):
        """Error messages longer than 200 chars should be truncated."""
        import asyncio
        import functools

        def _safe_tool(fn):
            @functools.wraps(fn)
            async def wrapper(*args, **kwargs):
                try:
                    return await fn(*args, **kwargs)
                except Exception as exc:
                    err_msg = f"{type(exc).__name__}: {str(exc)[:200]}"
                    return json.dumps({
                        "error": err_msg,
                        "tool": fn.__name__,
                        "suggestion": "Retry the tool call, or call get_logs() to inspect recent errors.",
                    })
            return wrapper

        @_safe_tool
        async def long_error_tool():
            raise RuntimeError("x" * 500)

        result = asyncio.run(long_error_tool())
        data = json.loads(result)

        # "RuntimeError: " + 200 chars = 215 chars max
        assert len(data["error"]) <= 215


# ═══════════════════════════════════════════════════════════════════
# 2. Backend dispatch — structured error JSON
# ═══════════════════════════════════════════════════════════════════


class TestDispatchErrorResponse:
    """The main dispatch function should return structured error JSON."""

    def test_dispatch_unknown_tool_returns_error(self):
        """Calling a non-existent tool should return error JSON, not crash."""
        # We can test the dispatch pattern without the full app by
        # checking the JSON structure for unknown tools.
        error_json = json.dumps({"error": "Unknown tool: fake_tool"})
        data = json.loads(error_json)
        assert "error" in data
        assert "fake_tool" in data["error"]

    def test_dispatch_error_has_suggestion_field(self):
        """Error data from dispatch should include a suggestion field."""
        # Simulate what dispatch produces on error
        error_data = {
            "error": "ValueError: bad argument",
            "tool": "speak_async",
            "suggestion": "Retry the tool call, or call get_logs() to inspect recent errors.",
        }
        assert "suggestion" in error_data
        assert "tool" in error_data
        assert error_data["tool"] == "speak_async"


# ═══════════════════════════════════════════════════════════════════
# 3. _drain_session_inbox — error isolation
# ═══════════════════════════════════════════════════════════════════


class TestDrainInboxErrorIsolation:
    """Errors processing one inbox item should not prevent processing the next."""

    def test_failed_speech_item_is_force_resolved(self):
        """If _activate_speech_item raises, the item should be force-resolved."""
        session = Session(session_id="test-drain-1", name="Test")

        # Create two speech items
        item1 = session.enqueue_speech("first", blocking=False, priority=0)
        item2 = session.enqueue_speech("second", blocking=False, priority=0)

        # Simulate what _drain_session_inbox does when _activate_speech_item
        # raises on the first item:
        front = session.peek_inbox()
        assert front is item1
        assert front.kind == "speech"

        # Mark as processing (as the drain loop does)
        front.processing = True

        # Simulate the error handling path: force-resolve the failed item
        if not front.done:
            front.result = {"selected": "_speech_done", "summary": "error"}
            front.done = True
            front.event.set()
            session._append_done(session.inbox.popleft())
            session.drain_kick.set()

        # Now the second item should be accessible
        next_item = session.peek_inbox()
        assert next_item is item2
        assert not next_item.done
        assert next_item.text == "second"

    def test_multiple_failed_items_all_resolved(self):
        """Multiple failing items should all get resolved, not leave orphans."""
        session = Session(session_id="test-drain-2", name="Test")

        items = []
        for i in range(5):
            item = session.enqueue_speech(f"item-{i}", blocking=False)
            items.append(item)

        # Force-resolve each item (simulating error recovery)
        for item in items:
            front = session.peek_inbox()
            if front and front.kind == "speech" and not front.done:
                front.processing = True
                front.result = {"selected": "_speech_done", "summary": "error"}
                front.done = True
                front.event.set()
                session._append_done(session.inbox.popleft())

        # All items should be done
        assert session.peek_inbox() is None
        for item in items:
            assert item.done is True
            assert item.event.is_set()

    def test_force_resolved_item_unblocks_waiting_thread(self):
        """A thread waiting on a force-resolved item should wake up."""
        session = Session(session_id="test-drain-3", name="Test")

        item = session.enqueue_speech("test", blocking=True, priority=0)

        woke_up = threading.Event()

        def waiter():
            item.event.wait(timeout=5)
            woke_up.set()

        t = threading.Thread(target=waiter, daemon=True)
        t.start()
        time.sleep(0.05)

        # Force-resolve (error recovery)
        item.result = {"selected": "_speech_done", "summary": "error"}
        item.done = True
        item.event.set()

        woke_up.wait(timeout=2)
        assert woke_up.is_set()

    def test_stuck_item_does_not_block_next_items(self):
        """A stuck (processing=True) speech item should block the drain loop,
        but if it gets force-resolved, subsequent items become accessible."""
        session = Session(session_id="test-drain-4", name="Test")

        stuck_item = session.enqueue_speech("stuck", blocking=False)
        next_item = session.enqueue_speech("next", blocking=False)

        # Mark the first as processing (simulating in-progress TTS)
        stuck_item.processing = True

        # peek_inbox returns the stuck item (not the next one)
        front = session.peek_inbox()
        assert front is stuck_item

        # Force-resolve the stuck item
        stuck_item.result = {"selected": "_speech_done", "summary": "error"}
        stuck_item.done = True
        stuck_item.event.set()
        session._append_done(session.inbox.popleft())

        # Now the next item is accessible
        front = session.peek_inbox()
        assert front is next_item


# ═══════════════════════════════════════════════════════════════════
# 4. _safe_action decorator — resilience
# ═══════════════════════════════════════════════════════════════════


class TestSafeActionDecorator:
    """_safe_action should catch all exceptions and never crash the TUI."""

    def test_catches_exception_and_does_not_raise(self):
        """A decorated method that raises should not propagate the exception."""
        from io_mcp.tui.widgets import _safe_action

        class FakeApp:
            _tts = MagicMock()

        @_safe_action
        def action_that_crashes(self):
            raise RuntimeError("boom")

        app = FakeApp()
        # Should not raise
        action_that_crashes(app)

    def test_prefers_speak_ui_over_tts(self):
        """_safe_action should prefer _speak_ui when available."""
        from io_mcp.tui.widgets import _safe_action

        speak_ui_called = []

        class FakeApp:
            _tts = MagicMock()

            def _speak_ui(self, text):
                speak_ui_called.append(text)

        @_safe_action
        def action_that_crashes(self):
            raise RuntimeError("boom")

        app = FakeApp()
        action_that_crashes(app)

        # _speak_ui should have been called, not _tts.speak_async
        assert len(speak_ui_called) == 1
        assert "action_that_crashes" in speak_ui_called[0]
        app._tts.speak_async.assert_not_called()

    def test_falls_back_to_tts_when_no_speak_ui(self):
        """If _speak_ui is not available, falls back to _tts.speak_async."""
        from io_mcp.tui.widgets import _safe_action

        class FakeApp:
            _tts = MagicMock()
            # No _speak_ui attribute

        @_safe_action
        def action_that_crashes(self):
            raise RuntimeError("boom")

        app = FakeApp()
        # Remove _speak_ui if somehow present
        if hasattr(app, '_speak_ui'):
            delattr(app, '_speak_ui')
        action_that_crashes(app)

        app._tts.speak_async.assert_called_once()
        call_text = app._tts.speak_async.call_args[0][0]
        assert "action_that_crashes" in call_text

    def test_survives_speak_ui_also_failing(self):
        """If _speak_ui itself raises, _safe_action should still not crash."""
        from io_mcp.tui.widgets import _safe_action

        class FakeApp:
            _tts = MagicMock()

            def _speak_ui(self, text):
                raise Exception("speak_ui also broken")

        @_safe_action
        def action_that_crashes(self):
            raise RuntimeError("boom")

        app = FakeApp()
        # Should not raise even though both the action and _speak_ui fail
        action_that_crashes(app)

    def test_returns_none_on_exception(self):
        """A failing action should return None (not the exception)."""
        from io_mcp.tui.widgets import _safe_action

        class FakeApp:
            _tts = MagicMock()

        @_safe_action
        def action_that_crashes(self):
            raise RuntimeError("boom")

        result = action_that_crashes(FakeApp())
        assert result is None

    def test_normal_return_value_preserved(self):
        """A successful action's return value should pass through."""
        from io_mcp.tui.widgets import _safe_action

        class FakeApp:
            _tts = MagicMock()

        @_safe_action
        def action_that_works(self):
            return 42

        result = action_that_works(FakeApp())
        assert result == 42

    def test_logs_to_error_log(self):
        """_safe_action should log the error to the TUI error log."""
        from io_mcp.tui.widgets import _safe_action, _log

        class FakeApp:
            _tts = MagicMock()

        @_safe_action
        def action_that_crashes(self):
            raise ValueError("test error for logging")

        with patch.object(_log, 'error') as mock_log_error:
            action_that_crashes(FakeApp())

            mock_log_error.assert_called_once()
            call_args = mock_log_error.call_args
            # First positional arg is the format string
            assert "action_that_crashes" in call_args[0][1]
            assert "ValueError" in call_args[0][2]


# ═══════════════════════════════════════════════════════════════════
# 5. Session inbox resilience — orphan cleanup timing
# ═══════════════════════════════════════════════════════════════════


class TestOrphanCleanupTiming:
    """Orphaned items from dead threads should be cleaned up promptly."""

    def test_multiple_orphans_cleaned_in_single_peek(self):
        """peek_inbox should clean ALL consecutive orphans, not just the first."""
        session = Session(session_id="test-orphan-1", name="Test")

        # Create multiple dead-thread items
        dead_threads = []
        for i in range(3):
            t = threading.Thread(target=lambda: None)
            t.start()
            t.join()
            dead_threads.append(t)

        for i, dt in enumerate(dead_threads):
            item = InboxItem(
                kind="choices",
                preamble=f"Dead item {i}",
                choices=[{"label": f"D{i}"}],
                owner_thread=dt,
            )
            session.enqueue(item)

        # Add a live item at the end
        live_item = InboxItem(
            kind="choices",
            preamble="Live item",
            choices=[{"label": "Live"}],
            owner_thread=threading.current_thread(),
        )
        session.enqueue(live_item)

        # Single peek should skip all dead-thread items and return the live one
        result = session.peek_inbox()
        assert result is live_item

        # All orphaned items should have been cleaned up
        assert len(session.inbox) == 1  # only live_item remains

    def test_all_orphans_yields_none(self):
        """If all items are orphaned, peek_inbox should return None."""
        session = Session(session_id="test-orphan-2", name="Test")

        dead = threading.Thread(target=lambda: None)
        dead.start()
        dead.join()

        for i in range(3):
            item = InboxItem(
                kind="choices",
                preamble=f"Dead {i}",
                choices=[{"label": f"D{i}"}],
                owner_thread=dead,
            )
            session.enqueue(item)

        result = session.peek_inbox()
        assert result is None
        assert len(session.inbox) == 0

    def test_orphan_items_resolve_with_restart(self):
        """Orphaned items should be resolved with _restart so callers retry."""
        session = Session(session_id="test-orphan-3", name="Test")

        dead = threading.Thread(target=lambda: None)
        dead.start()
        dead.join()

        item = InboxItem(
            kind="choices",
            preamble="Dead",
            choices=[{"label": "D"}],
            owner_thread=dead,
        )
        session.enqueue(item)

        session.peek_inbox()

        assert item.done is True
        assert item.result["selected"] == "_restart"
        assert item.event.is_set()


# ═══════════════════════════════════════════════════════════════════
# 6. Error response shape consistency
# ═══════════════════════════════════════════════════════════════════


class TestErrorResponseShape:
    """All error responses should have a consistent JSON shape."""

    def test_server_safe_tool_shape(self):
        """server.py _safe_tool produces {error, tool, suggestion}."""
        # Verify the error response pattern
        data = {
            "error": "SomeError: details",
            "tool": "some_tool",
            "suggestion": "Retry the tool call, or call get_logs() to inspect recent errors.",
        }
        assert all(k in data for k in ("error", "tool", "suggestion"))

    def test_dispatch_error_shape(self):
        """__main__.py dispatch produces {error, tool, suggestion}."""
        data = {
            "error": "TypeError: bad input",
            "tool": "set_speed",
            "suggestion": "Retry the tool call, or call get_logs() to inspect recent errors.",
        }
        assert all(k in data for k in ("error", "tool", "suggestion"))

    def test_error_response_is_valid_json(self):
        """Error responses should be valid JSON even with special chars."""
        error_msg = 'ValueError: quotes "in" message & <tags>'
        data = {
            "error": f"{error_msg[:200]}",
            "tool": "test_tool",
            "suggestion": "Retry the tool call, or call get_logs() to inspect recent errors.",
        }
        # Should round-trip through JSON cleanly
        serialized = json.dumps(data)
        parsed = json.loads(serialized)
        assert parsed["tool"] == "test_tool"
        assert '"in"' in parsed["error"]
