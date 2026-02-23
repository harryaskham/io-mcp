"""Backend HTTP handler for io-mcp.

Exposes /handle-mcp endpoint that the MCP proxy server calls.
Each request is a JSON object with {tool, args, session_id} and the
response is the tool's return value (a string).

Also exposes /health for the proxy to verify the backend is running.
"""

from __future__ import annotations

import json
import logging
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Any, Callable, Optional

log = logging.getLogger("io-mcp.backend")


class BackendHandler(BaseHTTPRequestHandler):
    """HTTP handler for /handle-mcp endpoint."""

    # Set by start_backend_server
    tool_dispatch: Callable[[str, dict, str], str] = None  # type: ignore

    def do_GET(self) -> None:
        if self.path == "/health":
            self._json_response(200, {"status": "ok"})
        else:
            self._json_response(404, {"error": "not found"})

    def do_POST(self) -> None:
        if self.path != "/handle-mcp":
            self._json_response(404, {"error": "not found"})
            return

        try:
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)
            request = json.loads(body)
        except (json.JSONDecodeError, ValueError) as e:
            self._json_response(400, {"error": f"Invalid JSON: {e}"})
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
) -> None:
    """Start the backend HTTP server in a daemon thread.

    Args:
        tool_dispatch: Function(tool_name, args, session_id) -> str
        host: Bind address (default: localhost only)
        port: Port number
    """
    # Bind dispatch function to handler class
    handler_class = type(
        "BoundBackendHandler",
        (BackendHandler,),
        {"tool_dispatch": staticmethod(tool_dispatch)},
    )

    server = HTTPServer((host, port), handler_class)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    log.info(f"Backend server started on {host}:{port}")
