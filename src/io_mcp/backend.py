"""Backend HTTP handler for io-mcp.

Exposes /handle-mcp endpoint that the MCP proxy server calls.
Each request is a JSON object with {tool, args, session_id} and the
response is the tool's return value (a string).

Also exposes simple REST endpoints for non-MCP callers:
  POST /speak          {"text": "...", "session_id": "..."}
  POST /speak-async    {"text": "...", "session_id": "..."}
  POST /choices        {"preamble": "...", "choices": [...], "session_id": "..."}
  POST /message        {"text": "...", "session_id": "..."}
  POST /inbox          {"session_id": "..."}
  GET  /health

These thin wrappers auto-create sessions on first use — no registration needed.
"""

from __future__ import annotations

import json
import logging
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from typing import Any, Callable, Optional

log = logging.getLogger("io-mcp.backend")


class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    """HTTPServer that handles each request in a new thread.

    Essential for io-mcp because present_choices blocks until the user
    makes a selection. Without threading, all other tool calls from all
    agents would be queued behind the blocking call.
    """
    daemon_threads = True


class BackendHandler(BaseHTTPRequestHandler):
    """HTTP handler for /handle-mcp endpoint."""

    # Set by start_backend_server
    tool_dispatch: Callable[[str, dict, str], str] = None  # type: ignore

    def do_GET(self) -> None:
        if self.path == "/health":
            self._json_response(200, {"status": "ok"})
        else:
            self._json_response(404, {"error": "not found"})

    # ─── Simple REST endpoint mapping ─────────────────────────────
    # Maps clean URL paths to MCP tool names + arg reshaping.
    # session_id defaults to "http-caller" if not provided.
    # Sessions are auto-created on first use — no registration needed.
    _SIMPLE_ENDPOINTS = {
        "/speak":       ("speak",           lambda b: {"text": b.get("text", "")}),
        "/speak-async": ("speak_async",     lambda b: {"text": b.get("text", "")}),
        "/choices":     ("present_choices",  lambda b: {"preamble": b.get("preamble", ""), "choices": b.get("choices", [])}),
        "/inbox":       ("check_inbox",      lambda b: {}),
    }

    def do_POST(self) -> None:
        # Parse body once for all endpoints
        try:
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)
            request = json.loads(body) if body else {}
        except (json.JSONDecodeError, ValueError) as e:
            self._json_response(400, {"error": f"Invalid JSON: {e}"})
            return

        # ── Simple REST endpoints (/speak, /choices, etc.) ────────
        if self.path in self._SIMPLE_ENDPOINTS:
            tool_name, arg_fn = self._SIMPLE_ENDPOINTS[self.path]
            session_id = request.get("session_id", "http-caller")
            args = arg_fn(request)
            try:
                result = self.tool_dispatch(tool_name, args, session_id)
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                response = result.encode("utf-8")
                self.send_header("Content-Length", str(len(response)))
                self.end_headers()
                self.wfile.write(response)
            except Exception as e:
                log.error(f"Simple endpoint error: {self.path}: {e}")
                self._json_response(500, {"error": str(e)[:200]})
            return

        # ── MCP proxy endpoint (/handle-mcp) ──────────────────────
        if self.path == "/cancel-mcp":
            # Cancel a pending tool call (e.g. present_choices aborted by client)
            tool = request.get("tool", "")
            session_id = request.get("session_id", "")
            try:
                cancel_fn = getattr(self, 'cancel_dispatch', None)
                if cancel_fn:
                    cancel_fn(tool, session_id)
                self._json_response(200, {"status": "cancelled"})
            except Exception as e:
                log.error(f"Cancel dispatch error: {tool}: {e}")
                self._json_response(500, {"error": str(e)[:200]})
            return

        if self.path == "/report-activity":
            # Lightweight activity report from hooks (fire-and-forget).
            # No full MCP dispatch — just log directly to the session.
            session_id = request.get("session_id", "")
            tool = request.get("tool", "")
            detail = request.get("detail", "")
            kind = request.get("kind", "tool")
            try:
                activity_fn = getattr(self, 'report_activity', None)
                if activity_fn:
                    activity_fn(session_id, tool, detail, kind)
                self._json_response(200, {"status": "logged"})
            except Exception as e:
                self._json_response(200, {"status": "ok"})  # Don't fail hooks
            return

        if self.path != "/handle-mcp":
            self._json_response(404, {"error": "not found"})
            return

        tool = request.get("tool", "")
        args = request.get("args", {})
        session_id = request.get("session_id", "")

        if not tool:
            self._json_response(400, {"error": "Missing 'tool' field"})
            return

        try:
            result = self.tool_dispatch(tool, args, session_id)
            # Return raw string result (the proxy sends it directly to the agent)
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            response = result.encode("utf-8")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)
        except Exception as e:
            log.error(f"Tool dispatch error: {tool}: {e}")
            self._json_response(500, {"error": str(e)[:200]})

    def _json_response(self, status: int, data: dict) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        body = json.dumps(data).encode()
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        """Suppress default HTTP logging (noisy, overwrites TUI)."""
        pass


def start_backend_server(
    tool_dispatch: Callable[[str, dict, str], str],
    host: str = "127.0.0.1",
    port: int = 8446,
    cancel_dispatch: Callable[[str, str], None] | None = None,
    report_activity: Callable[[str, str, str, str], None] | None = None,
) -> None:
    """Start the backend HTTP server in a daemon thread.

    Args:
        tool_dispatch: Function(tool_name, args, session_id) -> str
        host: Bind address (default: localhost only)
        port: Port number
        cancel_dispatch: Function(tool_name, session_id) -> None for MCP cancellation
        report_activity: Function(session_id, tool, detail, kind) -> None for hook activity
    """
    # Bind dispatch function to handler class
    attrs = {"tool_dispatch": staticmethod(tool_dispatch)}
    if cancel_dispatch:
        attrs["cancel_dispatch"] = staticmethod(cancel_dispatch)
    if report_activity:
        attrs["report_activity"] = staticmethod(report_activity)
    handler_class = type(
        "BoundBackendHandler",
        (BackendHandler,),
        attrs,
    )

    server = ThreadingHTTPServer((host, port), handler_class)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    log.info(f"Backend server started on {host}:{port}")
