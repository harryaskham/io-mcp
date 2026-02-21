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


class TestGetSessionId:
    """Test _get_session_id extracts mcp_session_id or falls back."""

    def test_extracts_mcp_session_id(self):
        from io_mcp.__main__ import _get_session_id

        class FakeCtx:
            class session:
                mcp_session_id = "deadbeef1234"

        assert _get_session_id(FakeCtx()) == "deadbeef1234"

    def test_fallback_to_id(self):
        from io_mcp.__main__ import _get_session_id

        class FakeSession:
            pass  # no mcp_session_id

        class FakeCtx:
            session = FakeSession()

        result = _get_session_id(FakeCtx())
        assert result == str(id(FakeCtx.session))
