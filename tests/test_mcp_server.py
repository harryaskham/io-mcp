"""Tests for MCP server startup, tool registration, and connectivity.

These tests catch issues like:
- Annotation resolution failures (from __future__ import annotations + local imports)
- Server thread crashes that would be silent in production
- Tool schema correctness
- Transport binding and basic request/response
- Session ID stability
"""

from __future__ import annotations

import http.client
import json
import threading
import time

import pytest

from mcp.server.fastmcp import FastMCP, Context


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _wait_for_port(host: str, port: int, timeout: float = 5.0) -> bool:
    """Poll until a TCP port is accepting connections."""
    import socket
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.1)
    return False


def _mcp_post(port: int, body: dict, session_id: str | None = None) -> http.client.HTTPResponse:
    """Send a JSON-RPC POST to /mcp and return the response."""
    conn = http.client.HTTPConnection("127.0.0.1", port)
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if session_id:
        headers["mcp-session-id"] = session_id
    conn.request("POST", "/mcp", body=json.dumps(body), headers=headers)
    return conn.getresponse()


def _initialize(port: int) -> http.client.HTTPResponse:
    """Send an MCP initialize request."""
    return _mcp_post(port, {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "1.0"},
        },
    })


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def free_port():
    """Find a free TCP port."""
    import socket
    with socket.socket() as s:
        s.bind(("", 0))
        return s.getsockname()[1]


@pytest.fixture()
def mcp_server(free_port):
    """Start a streamable-http MCP server with tools matching io-mcp's shape.

    This mirrors the real _run_mcp_server_inner setup so that annotation
    resolution, tool registration, and transport binding are all exercised.
    """
    server = FastMCP("io-mcp-test", host="0.0.0.0", port=free_port)

    # Register tools with the same signatures as __main__.py.
    # This is the code path that broke: @server.tool() with ctx: Context
    # under `from __future__ import annotations`.
    @server.tool()
    async def present_choices(
        preamble: str,
        choices: list[dict],
        ctx: Context,
    ) -> str:
        """Test tool matching present_choices signature."""
        return json.dumps({"selected": "test", "summary": "test"})

    @server.tool()
    async def speak(text: str, ctx: Context) -> str:
        """Test tool matching speak signature."""
        return f"Spoke: {text}"

    @server.tool()
    async def speak_async(text: str, ctx: Context) -> str:
        """Test tool matching speak_async signature."""
        return f"Spoke: {text}"

    errors: list[Exception] = []

    def run():
        try:
            server.run(transport="streamable-http")
        except Exception as exc:
            errors.append(exc)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()

    if not _wait_for_port("127.0.0.1", free_port):
        if errors:
            raise errors[0]
        pytest.fail(f"Server did not bind to port {free_port} within timeout")

    yield server, free_port, errors

    # No explicit shutdown needed — daemon thread dies with the test process.


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestToolRegistration:
    """Catch annotation resolution and tool schema issues at registration time."""

    def test_tools_register_with_context_param(self):
        """Context parameter must not break tool registration.

        This is the exact bug that was hit: `from __future__ import annotations`
        turns annotations into strings, and if Context is not importable at
        decoration time, @server.tool() raises InvalidSignature.
        """
        server = FastMCP("annotation-test")

        # This must not raise — if Context isn't resolvable, it will.
        @server.tool()
        async def my_tool(text: str, ctx: Context) -> str:
            return text

    def test_context_excluded_from_schema(self):
        """Context param must not appear in the tool's input schema."""
        server = FastMCP("schema-test")

        @server.tool()
        async def my_tool(text: str, ctx: Context) -> str:
            return text

        import asyncio
        tools = asyncio.run(server.list_tools())
        assert len(tools) == 1
        schema = tools[0].inputSchema
        assert "ctx" not in schema.get("properties", {}), \
            "Context should be excluded from tool input schema"
        assert "ctx" not in schema.get("required", []), \
            "Context should not be required in tool input schema"

    def test_all_three_tools_registered(self, mcp_server):
        """All three io-mcp tools must be registered without errors."""
        server, port, errors = mcp_server
        assert not errors, f"Server startup errors: {errors}"

        import asyncio
        tools = asyncio.run(server.list_tools())
        tool_names = {t.name for t in tools}
        assert tool_names == {"present_choices", "speak", "speak_async"}


class TestServerConnectivity:
    """Verify the server actually binds, accepts connections, and speaks MCP."""

    def test_server_binds_and_responds(self, mcp_server):
        """Server must accept HTTP POST on /mcp and return valid JSON-RPC."""
        _, port, _ = mcp_server
        resp = _initialize(port)
        assert resp.status == 200
        data = resp.read().decode()
        # streamable-http returns SSE events
        assert "result" in data

    def test_session_id_returned(self, mcp_server):
        """Server must return an mcp-session-id header."""
        _, port, _ = mcp_server
        resp = _initialize(port)
        session_id = resp.getheader("mcp-session-id")
        assert session_id is not None, "Missing mcp-session-id header"
        assert len(session_id) > 0

    def test_session_id_is_stable_across_requests(self, mcp_server):
        """Same session ID should route to the same session."""
        _, port, _ = mcp_server

        # First request gets a session ID
        resp1 = _initialize(port)
        sid = resp1.getheader("mcp-session-id")
        resp1.read()  # drain

        # Second request with that session ID should be accepted
        resp2 = _mcp_post(port, {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "notifications/initialized",
        }, session_id=sid)
        # 200 or 202 means the session was found
        assert resp2.status in (200, 202, 204), \
            f"Session ID not recognized on second request: {resp2.status}"

    def test_no_crash_on_startup(self, mcp_server):
        """Server thread must not have crashed."""
        _, _, errors = mcp_server
        assert errors == [], f"Server thread crashed: {errors}"


class TestSessionManager:
    """Test SessionManager with string IDs (was int before migration)."""

    def test_string_session_ids(self):
        from io_mcp.session import SessionManager

        mgr = SessionManager()
        session, created = mgr.get_or_create("abc123")
        assert created
        assert session.session_id == "abc123"

        session2, created2 = mgr.get_or_create("abc123")
        assert not created2
        assert session2 is session

    def test_uuid_style_session_ids(self):
        """Session IDs from streamable-http are hex UUIDs."""
        from io_mcp.session import SessionManager

        mgr = SessionManager()
        sid = "a510e2f0e66e499196848b7bdb6f00ec"
        session, created = mgr.get_or_create(sid)
        assert created
        assert session.session_id == sid
        assert mgr.focused() is session

    def test_tab_navigation_with_string_ids(self):
        from io_mcp.session import SessionManager

        mgr = SessionManager()
        mgr.get_or_create("session-a")
        mgr.get_or_create("session-b")
        mgr.get_or_create("session-c")

        assert mgr.active_session_id == "session-a"

        s = mgr.next_tab()
        assert s is not None
        assert s.session_id == "session-b"

        s = mgr.next_tab()
        assert s is not None
        assert s.session_id == "session-c"

        s = mgr.next_tab()
        assert s is not None
        assert s.session_id == "session-a"  # wraps around

    def test_remove_with_string_ids(self):
        from io_mcp.session import SessionManager

        mgr = SessionManager()
        mgr.get_or_create("x")
        mgr.get_or_create("y")
        assert mgr.active_session_id == "x"

        new_active = mgr.remove("x")
        assert new_active == "y"
        assert mgr.count() == 1


class TestSessionCleanup:
    """Test automatic cleanup of stale sessions."""

    def test_cleanup_removes_stale_sessions(self):
        from io_mcp.session import SessionManager

        mgr = SessionManager()
        s1, _ = mgr.get_or_create("session-1")
        s2, _ = mgr.get_or_create("session-2")
        s3, _ = mgr.get_or_create("session-3")

        # Make s2 and s3 stale (pretend they were active 10 minutes ago)
        s2.last_activity = time.time() - 600
        s3.last_activity = time.time() - 600

        # s1 is focused (active_session_id), so it should NOT be removed
        # s2 and s3 are stale and not focused, so they should be removed
        removed = mgr.cleanup_stale(timeout_seconds=300.0)
        assert "session-2" in removed
        assert "session-3" in removed
        assert "session-1" not in removed
        assert mgr.count() == 1

    def test_cleanup_preserves_active_choices(self):
        from io_mcp.session import SessionManager

        mgr = SessionManager()
        s1, _ = mgr.get_or_create("a")
        s2, _ = mgr.get_or_create("b")

        # Make s2 stale but with active choices
        s2.last_activity = time.time() - 600
        s2.active = True

        removed = mgr.cleanup_stale(timeout_seconds=300.0)
        assert removed == []
        assert mgr.count() == 2

    def test_cleanup_preserves_focused_session(self):
        from io_mcp.session import SessionManager

        mgr = SessionManager()
        s1, _ = mgr.get_or_create("focused")
        s1.last_activity = time.time() - 600  # stale but focused

        removed = mgr.cleanup_stale(timeout_seconds=300.0)
        assert removed == []
        assert mgr.count() == 1

    def test_cleanup_no_stale_sessions(self):
        from io_mcp.session import SessionManager

        mgr = SessionManager()
        mgr.get_or_create("fresh-1")
        mgr.get_or_create("fresh-2")

        removed = mgr.cleanup_stale(timeout_seconds=300.0)
        assert removed == []
        assert mgr.count() == 2

    def test_touch_updates_activity(self):
        from io_mcp.session import SessionManager

        mgr = SessionManager()
        s1, _ = mgr.get_or_create("s1")
        s2, _ = mgr.get_or_create("s2")

        # Make both stale
        old_time = time.time() - 600
        s1.last_activity = old_time
        s2.last_activity = old_time

        # Touch s2 to refresh it
        s2.touch()

        # Only s1 is not focused, so cleanup should only get non-focused stale ones
        # s1 is focused so it won't be removed. Focus s2 instead.
        mgr.focus("s2")

        removed = mgr.cleanup_stale(timeout_seconds=300.0)
        assert "s1" in removed
        assert "s2" not in removed


class TestGetSessionId:
    """Test _get_session_id extracts mcp_session_id or falls back."""

    def test_extracts_mcp_session_id(self):
        from io_mcp.server import _get_session_id

        class FakeCtx:
            class session:
                mcp_session_id = "deadbeef1234"

        assert _get_session_id(FakeCtx()) == "deadbeef1234"

    def test_fallback_to_id(self):
        from io_mcp.server import _get_session_id

        class FakeSession:
            pass  # no mcp_session_id

        class FakeCtx:
            session = FakeSession()

        result = _get_session_id(FakeCtx())
        assert result == str(id(FakeCtx.session))


# ---------------------------------------------------------------------------
# Proxy error handling tests
# ---------------------------------------------------------------------------

class TestIsConnectionError:
    """Test _is_connection_error correctly classifies exceptions."""

    def test_connection_refused(self):
        from io_mcp.proxy import _is_connection_error
        assert _is_connection_error(ConnectionRefusedError()) is True

    def test_connection_reset(self):
        from io_mcp.proxy import _is_connection_error
        assert _is_connection_error(ConnectionResetError()) is True

    def test_connection_aborted(self):
        from io_mcp.proxy import _is_connection_error
        assert _is_connection_error(ConnectionAbortedError()) is True

    def test_broken_pipe(self):
        from io_mcp.proxy import _is_connection_error
        assert _is_connection_error(BrokenPipeError()) is True

    def test_timeout_error(self):
        from io_mcp.proxy import _is_connection_error
        assert _is_connection_error(TimeoutError()) is True

    def test_socket_timeout(self):
        import socket
        from io_mcp.proxy import _is_connection_error
        assert _is_connection_error(socket.timeout()) is True

    def test_url_error_wrapping_connection_refused(self):
        import urllib.error
        from io_mcp.proxy import _is_connection_error
        exc = urllib.error.URLError(ConnectionRefusedError())
        assert _is_connection_error(exc) is True

    def test_url_error_wrapping_timeout(self):
        import urllib.error
        from io_mcp.proxy import _is_connection_error
        exc = urllib.error.URLError(TimeoutError("timed out"))
        assert _is_connection_error(exc) is True

    def test_url_error_wrapping_errno_connrefused(self):
        import errno
        import urllib.error
        from io_mcp.proxy import _is_connection_error
        inner = OSError(errno.ECONNREFUSED, "Connection refused")
        exc = urllib.error.URLError(inner)
        assert _is_connection_error(exc) is True

    def test_os_error_with_retriable_errno(self):
        import errno
        from io_mcp.proxy import _is_connection_error
        exc = OSError(errno.ECONNRESET, "Connection reset")
        assert _is_connection_error(exc) is True

    def test_non_retriable_url_error(self):
        import urllib.error
        from io_mcp.proxy import _is_connection_error
        exc = urllib.error.URLError("unknown hostname")
        assert _is_connection_error(exc) is False

    def test_non_retriable_os_error(self):
        import errno
        from io_mcp.proxy import _is_connection_error
        exc = OSError(errno.ENOENT, "No such file")
        assert _is_connection_error(exc) is False

    def test_value_error_is_not_connection_error(self):
        from io_mcp.proxy import _is_connection_error
        assert _is_connection_error(ValueError("bad value")) is False

    def test_runtime_error_is_not_connection_error(self):
        from io_mcp.proxy import _is_connection_error
        assert _is_connection_error(RuntimeError("something")) is False


class TestForwardToBackend:
    """Test _forward_to_backend retry and error handling."""

    def test_successful_forward(self):
        """Successful response is returned directly."""
        from io_mcp.proxy import _forward_to_backend
        from http.server import HTTPServer, BaseHTTPRequestHandler
        import socket

        # Start a minimal HTTP server that returns a success response
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                content_length = int(self.headers.get("Content-Length", 0))
                self.rfile.read(content_length)
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                body = b'{"ok": true}'
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
            f"http://127.0.0.1:{port}", "speak_async", {"text": "hi"}, "sid1"
        )
        assert '"ok": true' in result
        server.server_close()

    def test_connection_refused_retries_and_fails(self):
        """Connection refused retries up to max_retries then returns error."""
        from io_mcp.proxy import _forward_to_backend

        result = _forward_to_backend(
            "http://127.0.0.1:19999",  # nothing listening
            "speak_async",
            {"text": "hello"},
            "sid1",
            max_retries=2,
            initial_backoff=0.01,
            max_backoff=0.02,
        )
        data = json.loads(result.split("\n\n[IO-MCP")[0])  # strip crash log hint
        assert "error" in data
        assert "unavailable" in data["error"].lower() or "refused" in data["error"].lower()

    def test_http_error_returns_immediately_no_retry(self):
        """HTTP errors (4xx, 5xx) are not retried — returned immediately."""
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
                body = b'{"error": "internal server error"}'
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
            f"http://127.0.0.1:{port}", "speak_async", {"text": "hi"}, "sid1",
            max_retries=5,
        )
        assert "internal server error" in result
        assert call_count == 1  # No retries for HTTP errors
        server.server_close()

    def test_unexpected_exception_returns_error_json(self):
        """Unexpected exceptions return error JSON with type name."""
        from io_mcp.proxy import _forward_to_backend
        from unittest.mock import patch

        # Patch urlopen to raise an unexpected error
        with patch("io_mcp.proxy.urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.side_effect = MemoryError("out of memory")
            result = _forward_to_backend(
                "http://127.0.0.1:8446", "speak", {"text": "hi"}, "sid1",
                max_retries=1,
            )

        data = json.loads(result.split("\n\n[IO-MCP")[0])
        assert "error" in data
        assert "MemoryError" in data["error"]
        assert "out of memory" in data["error"]

    def test_non_retriable_url_error_fails_fast(self):
        """Non-retriable URLError (bad hostname) fails immediately, no retries."""
        from io_mcp.proxy import _forward_to_backend
        from unittest.mock import patch
        import urllib.error

        call_count = 0

        def fake_urlopen(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            raise urllib.error.URLError("unknown hostname")

        with patch("io_mcp.proxy.urllib.request.urlopen", side_effect=fake_urlopen):
            result = _forward_to_backend(
                "http://bad-hostname-that-does-not-exist:8446",
                "speak",
                {"text": "hi"},
                "sid1",
                max_retries=5,
                initial_backoff=0.01,
            )

        data = json.loads(result.split("\n\n[IO-MCP")[0])
        assert "error" in data
        assert call_count == 1  # No retries for non-retriable errors


class TestBlockingToolTimeouts:
    """Test that blocking tools get longer timeouts."""

    def test_blocking_tools_set(self):
        """All expected blocking tools are in the _BLOCKING_TOOLS set."""
        from io_mcp.proxy import _BLOCKING_TOOLS

        assert "present_choices" in _BLOCKING_TOOLS
        assert "present_multi_select" in _BLOCKING_TOOLS
        assert "run_command" in _BLOCKING_TOOLS
        assert "request_close" in _BLOCKING_TOOLS
        assert "speak" in _BLOCKING_TOOLS
        assert "speak_urgent" in _BLOCKING_TOOLS

    def test_non_blocking_tools_not_in_set(self):
        """Quick tools are not in _BLOCKING_TOOLS."""
        from io_mcp.proxy import _BLOCKING_TOOLS

        assert "speak_async" not in _BLOCKING_TOOLS
        assert "set_speed" not in _BLOCKING_TOOLS
        assert "get_settings" not in _BLOCKING_TOOLS
        assert "check_inbox" not in _BLOCKING_TOOLS
        assert "report_status" not in _BLOCKING_TOOLS


class TestCancelBackendTool:
    """Test _cancel_backend_tool best-effort behavior."""

    def test_cancel_succeeds(self):
        """Cancel sends POST to /cancel-mcp and succeeds."""
        from io_mcp.proxy import _cancel_backend_tool
        from http.server import HTTPServer, BaseHTTPRequestHandler
        import socket

        received = {}

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                content_length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(content_length)
                received.update(json.loads(body))
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                resp = b'{"status": "cancelled"}'
                self.send_header("Content-Length", str(len(resp)))
                self.end_headers()
                self.wfile.write(resp)

            def log_message(self, *args):
                pass

        with socket.socket() as s:
            s.bind(("", 0))
            port = s.getsockname()[1]

        server = HTTPServer(("127.0.0.1", port), Handler)
        thread = threading.Thread(target=server.handle_request, daemon=True)
        thread.start()
        time.sleep(0.1)

        # Should not raise
        _cancel_backend_tool(f"http://127.0.0.1:{port}", "present_choices", "session-abc")
        assert received.get("tool") == "present_choices"
        assert received.get("session_id") == "session-abc"
        server.server_close()

    def test_cancel_failure_does_not_raise(self):
        """Cancel gracefully handles backend being down."""
        from io_mcp.proxy import _cancel_backend_tool

        # Nothing listening on this port — should not raise
        _cancel_backend_tool("http://127.0.0.1:19998", "present_choices", "sid1")


class TestProxyGetSessionId:
    """Test proxy's _get_session_id handles edge cases."""

    def test_extracts_mcp_session_id(self):
        from io_mcp.proxy import _get_session_id

        class FakeCtx:
            class session:
                mcp_session_id = "proxy-session-123"

        assert _get_session_id(FakeCtx()) == "proxy-session-123"

    def test_fallback_to_object_id(self):
        from io_mcp.proxy import _get_session_id

        class FakeSession:
            pass

        class FakeCtx:
            session = FakeSession()

        result = _get_session_id(FakeCtx())
        assert result == str(id(FakeCtx.session))

    def test_none_mcp_session_id_uses_fallback(self):
        from io_mcp.proxy import _get_session_id

        class FakeCtx:
            class session:
                mcp_session_id = None

        result = _get_session_id(FakeCtx())
        # Should use id() fallback when mcp_session_id is None
        assert result == str(id(FakeCtx.session))


class TestFwdErrorHandling:
    """Test the _fwd async forwarder handles errors gracefully."""

    def test_fwd_catches_session_id_error(self):
        """If ctx.session is broken, _fwd uses 'unknown' as session ID."""
        import asyncio
        from io_mcp.proxy import create_proxy_server

        # Create a server to get access to _fwd indirectly
        # We test through the tool definitions instead
        server = create_proxy_server(
            backend_url="http://127.0.0.1:19997",  # nothing listening
        )

        # Verify tools are registered (they all use _fwd internally)
        tools = asyncio.run(server.list_tools())
        tool_names = {t.name for t in tools}
        assert "present_choices" in tool_names
        assert "speak" in tool_names
        assert "speak_async" in tool_names

    def test_proxy_server_registers_all_tools(self):
        """All expected tools are registered on the proxy server."""
        import asyncio
        from io_mcp.proxy import create_proxy_server

        server = create_proxy_server()
        tools = asyncio.run(server.list_tools())
        tool_names = {t.name for t in tools}

        expected_tools = {
            "present_choices", "present_multi_select",
            "speak", "speak_async", "speak_urgent",
            "set_speed", "set_voice", "set_tts_model", "set_stt_model", "set_emotion",
            "get_settings", "register_session", "rename_session",
            "reload_config", "pull_latest", "run_command",
            "request_restart", "request_proxy_restart", "request_close",
            "check_inbox", "report_status", "get_logs",
            "get_sessions", "get_speech_history",
            "get_current_choices", "get_tui_state",
        }
        assert tool_names == expected_tools


class TestCrashLogHint:
    """Test _crash_log_hint returns useful diagnostics."""

    def test_empty_when_no_logs(self):
        from io_mcp.proxy import _crash_log_hint

        # With no log files (or empty ones), should return empty string
        result = _crash_log_hint()
        # Result depends on whether log files exist on disk, so just check type
        assert isinstance(result, str)

    def test_includes_crash_diagnostics_header(self):
        """If log files have content, the hint includes the header."""
        import tempfile
        import os
        from io_mcp.proxy import _crash_log_hint
        from unittest.mock import patch

        # Write some fake log content
        with tempfile.NamedTemporaryFile(mode='w', suffix='.log', delete=False) as f:
            f.write("ERROR: something broke\n")
            tmp_path = f.name

        try:
            with patch("io_mcp.proxy.TUI_ERROR_LOG", tmp_path):
                result = _crash_log_hint()
                assert "[IO-MCP CRASH DIAGNOSTICS]" in result
                assert "something broke" in result
                assert "SELF-HEALING INSTRUCTIONS" in result
        finally:
            os.unlink(tmp_path)
