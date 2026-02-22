"""Frontend API for remote clients (Android app, web, etc.).

Exposes a REST + SSE API alongside the MCP server so that a remote
frontend can receive events and send user actions without sharing
the same process.

Endpoints:
  GET  /api/events          SSE stream of frontend events
  GET  /api/sessions        List active sessions
  GET  /api/sessions/:id    Get session state
  GET  /api/settings        Current settings
  POST /api/sessions/:id/select   Send a selection
  POST /api/sessions/:id/message  Queue a user message
  POST /api/settings/speed        Set TTS speed
  POST /api/settings/voice        Set TTS voice
  POST /api/settings/emotion      Set TTS emotion

Events (SSE):
  choices_presented  New choices for a session
  speech_requested   TTS narration requested
  session_created    New session tab opened
  session_removed    Session tab closed
  settings_changed   Settings updated
"""

from __future__ import annotations

import asyncio
import json
import logging
import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional

log = logging.getLogger("io-mcp.api")


@dataclass
class FrontendEvent:
    """An event to push to remote frontends via SSE."""
    event_type: str
    data: dict[str, Any]
    session_id: Optional[str] = None
    timestamp: float = field(default_factory=time.time)

    def to_sse(self) -> str:
        """Format as Server-Sent Events message."""
        payload = json.dumps({
            "type": self.event_type,
            "session_id": self.session_id,
            "data": self.data,
            "timestamp": self.timestamp,
        })
        return f"event: {self.event_type}\ndata: {payload}\n\n"


class EventBus:
    """Thread-safe event bus for pushing events to SSE subscribers.

    Multiple SSE clients can subscribe. Events are broadcast to all.
    Subscribers get their own queue with a max size to prevent memory issues.
    """

    def __init__(self, max_queue_size: int = 100):
        self._subscribers: list[queue.Queue] = []
        self._lock = threading.Lock()
        self._max_queue_size = max_queue_size

    def subscribe(self) -> queue.Queue:
        """Create a new subscriber queue."""
        q: queue.Queue = queue.Queue(maxsize=self._max_queue_size)
        with self._lock:
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        """Remove a subscriber queue."""
        with self._lock:
            self._subscribers = [s for s in self._subscribers if s is not q]

    def publish(self, event: FrontendEvent) -> None:
        """Publish an event to all subscribers."""
        with self._lock:
            dead = []
            for q in self._subscribers:
                try:
                    q.put_nowait(event)
                except queue.Full:
                    # Drop oldest event and retry
                    try:
                        q.get_nowait()
                        q.put_nowait(event)
                    except (queue.Empty, queue.Full):
                        dead.append(q)
            # Clean up dead subscribers
            for q in dead:
                self._subscribers.remove(q)

    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscribers)


# Global event bus instance
event_bus = EventBus()


def emit_choices_presented(session_id: str, preamble: str, choices: list[dict]) -> None:
    """Emit event when choices are presented to a session."""
    event_bus.publish(FrontendEvent(
        event_type="choices_presented",
        session_id=session_id,
        data={"preamble": preamble, "choices": choices},
    ))


def emit_speech_requested(session_id: str, text: str, blocking: bool = False,
                          priority: int = 0) -> None:
    """Emit event when speech is requested for a session."""
    event_bus.publish(FrontendEvent(
        event_type="speech_requested",
        session_id=session_id,
        data={"text": text, "blocking": blocking, "priority": priority},
    ))


def emit_session_created(session_id: str, name: str) -> None:
    """Emit event when a new session is created."""
    event_bus.publish(FrontendEvent(
        event_type="session_created",
        session_id=session_id,
        data={"name": name},
    ))


def emit_session_removed(session_id: str) -> None:
    """Emit event when a session is removed."""
    event_bus.publish(FrontendEvent(
        event_type="session_removed",
        session_id=session_id,
        data={},
    ))


def emit_settings_changed(settings: dict[str, Any]) -> None:
    """Emit event when settings change."""
    event_bus.publish(FrontendEvent(
        event_type="settings_changed",
        data=settings,
    ))


def emit_selection_made(session_id: str, label: str, summary: str) -> None:
    """Emit event when a selection is made."""
    event_bus.publish(FrontendEvent(
        event_type="selection_made",
        session_id=session_id,
        data={"label": label, "summary": summary},
    ))


# ─── HTTP Server for Frontend API ────────────────────────────────────────

import http.server
import urllib.parse


class FrontendAPIHandler(http.server.BaseHTTPRequestHandler):
    """HTTP handler for the frontend API."""

    def log_message(self, format, *args):
        pass

    def _send_json(self, data: Any, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        body = self.rfile.read(length)
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            return {}

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path == "/api/events":
            self._handle_sse()
        elif path == "/api/sessions":
            self._handle_list_sessions()
        elif path == "/api/settings":
            self._handle_get_settings()
        elif path == "/api/health":
            self._handle_health()
        else:
            self._send_json({"error": "not found"}, 404)

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        body = self._read_body()

        if path.startswith("/api/sessions/") and path.endswith("/select"):
            session_id = path.split("/")[-2]
            self._handle_select(session_id, body)
        elif path.startswith("/api/sessions/") and path.endswith("/message"):
            session_id = path.split("/")[-2]
            self._handle_message(session_id, body)
        else:
            self._send_json({"error": "not found"}, 404)

    def _handle_sse(self) -> None:
        """Stream Server-Sent Events to the client."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

        sub = event_bus.subscribe()
        try:
            self.wfile.write(b"event: connected\ndata: {}\n\n")
            self.wfile.flush()

            while True:
                try:
                    event = sub.get(timeout=30)
                    self.wfile.write(event.to_sse().encode())
                    self.wfile.flush()
                except queue.Empty:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            event_bus.unsubscribe(sub)

    def _handle_list_sessions(self) -> None:
        frontend = getattr(self.server, 'frontend', None)
        if not frontend:
            self._send_json({"error": "no frontend"}, 500)
            return
        sessions = []
        for s in frontend.manager.all_sessions():
            sessions.append({
                "id": s.session_id,
                "name": s.name,
                "active": s.active,
                "preamble": s.preamble if s.active else "",
                "choices": s.choices if s.active else [],
            })
        self._send_json({"sessions": sessions})

    def _handle_get_settings(self) -> None:
        frontend = getattr(self.server, 'frontend', None)
        if not frontend or not frontend.config:
            self._send_json({"error": "no config"}, 500)
            return
        cfg = frontend.config
        self._send_json({
            "tts_model": cfg.tts_model_name,
            "tts_voice": cfg.tts_voice,
            "tts_speed": cfg.tts_speed,
            "tts_emotion": cfg.tts_emotion,
            "stt_model": cfg.stt_model_name,
        })

    def _handle_health(self) -> None:
        frontend = getattr(self.server, 'frontend', None)
        session_count = frontend.manager.count() if frontend else 0
        self._send_json({
            "status": "ok",
            "sessions": session_count,
            "sse_subscribers": event_bus.subscriber_count(),
        })

    def _handle_select(self, session_id: str, body: dict) -> None:
        frontend = getattr(self.server, 'frontend', None)
        if not frontend:
            self._send_json({"error": "no frontend"}, 500)
            return
        session = frontend.manager.get(session_id)
        if not session or not session.active:
            self._send_json({"error": "session not found or inactive"}, 404)
            return
        label = body.get("label", "")
        summary = body.get("summary", "")
        session.selection = {"selected": label, "summary": summary}
        session.selection_event.set()
        self._send_json({"status": "selected", "label": label})

    def _handle_message(self, session_id: str, body: dict) -> None:
        frontend = getattr(self.server, 'frontend', None)
        if not frontend:
            self._send_json({"error": "no frontend"}, 500)
            return
        session = frontend.manager.get(session_id)
        if not session:
            self._send_json({"error": "session not found"}, 404)
            return
        text = body.get("text", "")
        if not text:
            self._send_json({"error": "no text"}, 400)
            return
        msgs = getattr(session, 'pending_messages', None)
        if msgs is not None:
            msgs.append(text)
        self._send_json({"status": "queued", "pending": len(msgs) if msgs else 0})


def start_api_server(frontend: Any, port: int = 8445, host: str = "0.0.0.0") -> threading.Thread:
    """Start the frontend API HTTP server in a background thread."""
    server = http.server.HTTPServer((host, port), FrontendAPIHandler)
    server.frontend = frontend  # type: ignore
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    print(f"  Frontend API: http://{host}:{port}/api/events (SSE)", flush=True)
    return thread
