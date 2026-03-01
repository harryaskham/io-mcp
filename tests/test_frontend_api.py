"""Comprehensive tests for the Frontend API module (src/io_mcp/api.py).

Tests cover:
1. EventBus — subscribe/unsubscribe, publish, queue overflow, dead subscriber cleanup
2. FrontendEvent — to_sse() formatting, field serialization
3. Emit helper functions — all emit_* create correct events
4. FrontendAPIHandler — every HTTP endpoint
5. Error cases — 404, 400, 500 responses
"""

from __future__ import annotations

import http.client
import http.server
import io
import json
import queue
import socket
import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from io_mcp.api import (
    EventBus,
    FrontendAPIHandler,
    FrontendEvent,
    emit_choices_presented,
    emit_recording_state,
    emit_selection_made,
    emit_session_created,
    emit_session_removed,
    emit_settings_changed,
    emit_speech_requested,
    event_bus,
    start_api_server,
)
from io_mcp.session import Session, SessionManager


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _free_port() -> int:
    """Find an available TCP port."""
    with socket.socket() as s:
        s.bind(("", 0))
        return s.getsockname()[1]


def _wait_for_port(host: str, port: int, timeout: float = 5.0) -> bool:
    """Poll until a TCP port is accepting connections."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.05)
    return False


def _make_session(session_id: str = "sess-1", name: str = "Agent 1",
                  active: bool = False, preamble: str = "",
                  choices: list | None = None) -> Session:
    """Create a Session with specified fields."""
    s = Session(session_id=session_id, name=name)
    s.active = active
    s.preamble = preamble
    if choices is not None:
        s.choices = choices
    return s


def _make_frontend(sessions: list[Session] | None = None,
                   config: object | None = None,
                   focused: Session | None = None) -> SimpleNamespace:
    """Create a mock frontend with a SessionManager populated with sessions."""
    manager = SessionManager()
    if sessions:
        for s in sessions:
            manager.sessions[s.session_id] = s
            manager.session_order.append(s.session_id)
        manager.active_session_id = sessions[0].session_id
    if focused:
        manager.active_session_id = focused.session_id
    frontend = SimpleNamespace(manager=manager, config=config)
    return frontend


def _make_config(**kwargs) -> SimpleNamespace:
    """Create a mock config object with TTS/STT attributes."""
    defaults = {
        "tts_model_name": "gpt-4o-mini-tts",
        "tts_voice": "sage",
        "tts_speed": 1.2,
        "tts_emotion": "calm",
        "stt_model_name": "whisper",
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


class _APITestServer:
    """Helper to start/stop a FrontendAPIHandler server for testing."""

    def __init__(self, frontend=None, highlight_callback=None, key_callback=None):
        self.port = _free_port()
        self.server = http.server.HTTPServer(("127.0.0.1", self.port), FrontendAPIHandler)
        if frontend is not None:
            self.server.frontend = frontend
        if highlight_callback is not None:
            self.server._highlight_callback = highlight_callback
        if key_callback is not None:
            self.server._key_callback = key_callback
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        assert _wait_for_port("127.0.0.1", self.port), f"Server didn't bind to {self.port}"

    def get(self, path: str) -> tuple[int, dict]:
        """Send GET request and return (status, json_body)."""
        conn = http.client.HTTPConnection("127.0.0.1", self.port)
        conn.request("GET", path)
        resp = conn.getresponse()
        status = resp.status
        data = resp.read().decode()
        try:
            return status, json.loads(data)
        except json.JSONDecodeError:
            return status, {"_raw": data}

    def post(self, path: str, body: dict | None = None) -> tuple[int, dict]:
        """Send POST request with JSON body and return (status, json_body)."""
        conn = http.client.HTTPConnection("127.0.0.1", self.port)
        payload = json.dumps(body) if body else ""
        headers = {"Content-Type": "application/json"}
        if payload:
            headers["Content-Length"] = str(len(payload))
        conn.request("POST", path, body=payload, headers=headers)
        resp = conn.getresponse()
        status = resp.status
        data = resp.read().decode()
        try:
            return status, json.loads(data)
        except json.JSONDecodeError:
            return status, {"_raw": data}

    def options(self, path: str) -> tuple[int, dict]:
        """Send OPTIONS request and return (status, headers_dict)."""
        conn = http.client.HTTPConnection("127.0.0.1", self.port)
        conn.request("OPTIONS", path)
        resp = conn.getresponse()
        status = resp.status
        resp.read()  # drain body
        headers = {k.lower(): v for k, v in resp.getheaders()}
        return status, headers

    def get_sse_stream(self, path: str = "/api/events", timeout: float = 0.5) -> tuple[int, str, dict]:
        """Open SSE stream, read initial data, return (status, body_chunk, headers).

        Uses a raw socket with a short timeout to avoid blocking on the
        persistent SSE connection.
        """
        s = socket.create_connection(("127.0.0.1", self.port), timeout=timeout)
        request = f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\nAccept: text/event-stream\r\n\r\n"
        s.sendall(request.encode())
        # Read with timeout — SSE sends "connected" event immediately
        time.sleep(0.15)
        data = b""
        try:
            while True:
                chunk = s.recv(4096)
                if not chunk:
                    break
                data += chunk
        except socket.timeout:
            pass
        s.close()

        text = data.decode("utf-8", errors="replace")
        # Parse status from first line
        first_line = text.split("\r\n")[0]
        status = int(first_line.split(" ")[1])
        # Parse headers
        header_section, _, body = text.partition("\r\n\r\n")
        headers = {}
        for line in header_section.split("\r\n")[1:]:
            if ": " in line:
                k, v = line.split(": ", 1)
                headers[k.lower()] = v
        return status, body, headers

    def shutdown(self):
        self.server.shutdown()


@pytest.fixture()
def api_server():
    """Fixture that creates an _APITestServer, shuts down after use."""
    servers: list[_APITestServer] = []

    def _factory(frontend=None, highlight_callback=None, key_callback=None):
        s = _APITestServer(frontend=frontend, highlight_callback=highlight_callback,
                           key_callback=key_callback)
        servers.append(s)
        return s

    yield _factory

    for s in servers:
        s.shutdown()


# ===========================================================================
# 1. EventBus tests
# ===========================================================================

class TestEventBus:
    """Tests for the EventBus pub/sub system."""

    def test_subscribe_returns_queue(self):
        bus = EventBus()
        q = bus.subscribe()
        assert isinstance(q, queue.Queue)
        assert bus.subscriber_count() == 1

    def test_unsubscribe_removes_queue(self):
        bus = EventBus()
        q = bus.subscribe()
        assert bus.subscriber_count() == 1
        bus.unsubscribe(q)
        assert bus.subscriber_count() == 0

    def test_unsubscribe_nonexistent_is_noop(self):
        bus = EventBus()
        q: queue.Queue = queue.Queue()
        bus.unsubscribe(q)  # should not raise
        assert bus.subscriber_count() == 0

    def test_publish_delivers_to_subscriber(self):
        bus = EventBus()
        q = bus.subscribe()
        event = FrontendEvent(event_type="test", data={"key": "value"})
        bus.publish(event)
        result = q.get_nowait()
        assert result is event
        assert result.event_type == "test"
        assert result.data == {"key": "value"}

    def test_publish_broadcasts_to_multiple_subscribers(self):
        bus = EventBus()
        q1 = bus.subscribe()
        q2 = bus.subscribe()
        q3 = bus.subscribe()
        event = FrontendEvent(event_type="broadcast", data={"n": 1})
        bus.publish(event)
        for q in (q1, q2, q3):
            result = q.get_nowait()
            assert result is event

    def test_publish_does_not_block_on_full_queue(self):
        """When queue is full, oldest event should be dropped and new event added."""
        bus = EventBus(max_queue_size=2)
        q = bus.subscribe()
        e1 = FrontendEvent(event_type="e1", data={})
        e2 = FrontendEvent(event_type="e2", data={})
        e3 = FrontendEvent(event_type="e3", data={})
        bus.publish(e1)
        bus.publish(e2)
        # Queue is now full (size 2). Publish a 3rd — should drop e1.
        bus.publish(e3)
        assert bus.subscriber_count() == 1  # subscriber still alive
        # e1 was dropped, queue has e2, e3
        result1 = q.get_nowait()
        result2 = q.get_nowait()
        assert result1.event_type == "e2"
        assert result2.event_type == "e3"

    def test_dead_subscriber_cleanup(self):
        """Subscriber that's completely broken gets removed on publish."""
        bus = EventBus(max_queue_size=1)
        q = bus.subscribe()
        assert bus.subscriber_count() == 1

        # Fill the queue completely
        bus.publish(FrontendEvent(event_type="fill", data={}))

        # Now monkey-patch the queue so get_nowait and put_nowait both fail
        # (simulating a broken subscriber)
        original_get = q.get_nowait
        original_put = q.put_nowait

        def fail_get(*args, **kwargs):
            raise queue.Empty()

        def fail_put(*args, **kwargs):
            raise queue.Full()

        q.get_nowait = fail_get
        q.put_nowait = fail_put

        # Publishing now should try to drop oldest (fails), then add (fails) → dead
        bus.publish(FrontendEvent(event_type="trigger_cleanup", data={}))
        assert bus.subscriber_count() == 0

    def test_publish_with_no_subscribers_is_noop(self):
        bus = EventBus()
        event = FrontendEvent(event_type="orphan", data={})
        bus.publish(event)  # should not raise

    def test_subscriber_count(self):
        bus = EventBus()
        assert bus.subscriber_count() == 0
        q1 = bus.subscribe()
        assert bus.subscriber_count() == 1
        q2 = bus.subscribe()
        assert bus.subscriber_count() == 2
        bus.unsubscribe(q1)
        assert bus.subscriber_count() == 1
        bus.unsubscribe(q2)
        assert bus.subscriber_count() == 0

    def test_thread_safety_subscribe_unsubscribe(self):
        """Multiple threads subscribing/unsubscribing concurrently shouldn't crash."""
        bus = EventBus()
        queues: list[queue.Queue] = []
        lock = threading.Lock()

        def subscribe_worker():
            q = bus.subscribe()
            with lock:
                queues.append(q)
            time.sleep(0.01)
            bus.unsubscribe(q)

        threads = [threading.Thread(target=subscribe_worker) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        # All should have unsubscribed
        assert bus.subscriber_count() == 0

    def test_thread_safety_publish(self):
        """Multiple threads publishing concurrently shouldn't crash."""
        bus = EventBus()
        q = bus.subscribe()
        received = []

        def publish_worker(n: int):
            bus.publish(FrontendEvent(event_type=f"thread-{n}", data={"n": n}))

        threads = [threading.Thread(target=publish_worker, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        # All 10 events should be delivered
        while not q.empty():
            received.append(q.get_nowait())
        assert len(received) == 10


# ===========================================================================
# 2. FrontendEvent tests
# ===========================================================================

class TestFrontendEvent:
    """Tests for FrontendEvent dataclass and to_sse() formatting."""

    def test_default_fields(self):
        e = FrontendEvent(event_type="test", data={"hello": "world"})
        assert e.event_type == "test"
        assert e.data == {"hello": "world"}
        assert e.session_id is None
        assert isinstance(e.timestamp, float)
        assert e.timestamp > 0

    def test_all_fields_set(self):
        ts = 1700000000.0
        e = FrontendEvent(
            event_type="choices_presented",
            data={"preamble": "Pick one", "choices": [{"label": "A"}]},
            session_id="sess-42",
            timestamp=ts,
        )
        assert e.event_type == "choices_presented"
        assert e.session_id == "sess-42"
        assert e.timestamp == ts
        assert e.data["preamble"] == "Pick one"

    def test_to_sse_format(self):
        ts = 1700000000.5
        e = FrontendEvent(
            event_type="speech_requested",
            data={"text": "Hello"},
            session_id="s1",
            timestamp=ts,
        )
        sse = e.to_sse()
        # SSE format: "event: <type>\ndata: <json>\n\n"
        assert sse.startswith("event: speech_requested\n")
        assert "data: " in sse
        assert sse.endswith("\n\n")

        # Extract the JSON payload from the data line
        data_line = sse.split("\n")[1]
        assert data_line.startswith("data: ")
        payload = json.loads(data_line[len("data: "):])
        assert payload["type"] == "speech_requested"
        assert payload["session_id"] == "s1"
        assert payload["timestamp"] == ts
        assert payload["data"] == {"text": "Hello"}

    def test_to_sse_null_session_id(self):
        e = FrontendEvent(event_type="settings_changed", data={"speed": 1.5})
        sse = e.to_sse()
        payload = json.loads(sse.split("\n")[1][len("data: "):])
        assert payload["session_id"] is None

    def test_to_sse_complex_data(self):
        """Nested dicts and lists serialize correctly."""
        data = {
            "choices": [
                {"label": "Option A", "summary": "First option"},
                {"label": "Option B", "summary": "Second option"},
            ],
            "metadata": {"nested": True, "count": 2},
        }
        e = FrontendEvent(event_type="complex", data=data, session_id="cx")
        sse = e.to_sse()
        payload = json.loads(sse.split("\n")[1][len("data: "):])
        assert payload["data"]["choices"][1]["label"] == "Option B"
        assert payload["data"]["metadata"]["nested"] is True

    def test_to_sse_encoding(self):
        """to_sse() returns a str that can be .encode()'d for HTTP response."""
        e = FrontendEvent(event_type="test", data={"emoji": "🎉"})
        sse = e.to_sse()
        encoded = sse.encode("utf-8")
        assert isinstance(encoded, bytes)
        # json.dumps uses ASCII-safe escapes for non-ASCII by default,
        # so the emoji appears as \\uXXXX sequences — verify roundtrip
        payload = json.loads(sse.split("\n")[1][len("data: "):])
        assert payload["data"]["emoji"] == "🎉"


# ===========================================================================
# 3. Emit helper function tests
# ===========================================================================

class TestEmitHelpers:
    """Tests for all emit_* convenience functions."""

    def _capture_event(self, bus: EventBus) -> FrontendEvent:
        """Subscribe, call a function, return the published event."""
        q = bus.subscribe()
        return q  # caller will trigger publish, then q.get_nowait()

    def test_emit_choices_presented(self):
        bus = EventBus()
        q = bus.subscribe()
        choices = [{"label": "A", "summary": "First"}, {"label": "B", "summary": "Second"}]
        with patch("io_mcp.api.event_bus", bus):
            emit_choices_presented("sid-1", "Pick one", choices)
        event = q.get_nowait()
        assert event.event_type == "choices_presented"
        assert event.session_id == "sid-1"
        assert event.data["preamble"] == "Pick one"
        assert len(event.data["choices"]) == 2
        assert event.data["choices"][0]["label"] == "A"

    def test_emit_speech_requested(self):
        bus = EventBus()
        q = bus.subscribe()
        with patch("io_mcp.api.event_bus", bus):
            emit_speech_requested("sid-2", "Hello world", blocking=True, priority=1)
        event = q.get_nowait()
        assert event.event_type == "speech_requested"
        assert event.session_id == "sid-2"
        assert event.data["text"] == "Hello world"
        assert event.data["blocking"] is True
        assert event.data["priority"] == 1

    def test_emit_speech_requested_defaults(self):
        bus = EventBus()
        q = bus.subscribe()
        with patch("io_mcp.api.event_bus", bus):
            emit_speech_requested("sid-3", "Test")
        event = q.get_nowait()
        assert event.data["blocking"] is False
        assert event.data["priority"] == 0

    def test_emit_session_created(self):
        bus = EventBus()
        q = bus.subscribe()
        with patch("io_mcp.api.event_bus", bus):
            emit_session_created("sid-new", "My Agent")
        event = q.get_nowait()
        assert event.event_type == "session_created"
        assert event.session_id == "sid-new"
        assert event.data["name"] == "My Agent"

    def test_emit_session_removed(self):
        bus = EventBus()
        q = bus.subscribe()
        with patch("io_mcp.api.event_bus", bus):
            emit_session_removed("sid-gone")
        event = q.get_nowait()
        assert event.event_type == "session_removed"
        assert event.session_id == "sid-gone"
        assert event.data == {}

    def test_emit_settings_changed(self):
        bus = EventBus()
        q = bus.subscribe()
        with patch("io_mcp.api.event_bus", bus):
            emit_settings_changed({"speed": 1.5, "voice": "nova"})
        event = q.get_nowait()
        assert event.event_type == "settings_changed"
        assert event.session_id is None
        assert event.data["speed"] == 1.5
        assert event.data["voice"] == "nova"

    def test_emit_selection_made(self):
        bus = EventBus()
        q = bus.subscribe()
        with patch("io_mcp.api.event_bus", bus):
            emit_selection_made("sid-x", "Option A", "First option")
        event = q.get_nowait()
        assert event.event_type == "selection_made"
        assert event.session_id == "sid-x"
        assert event.data["label"] == "Option A"
        assert event.data["summary"] == "First option"

    def test_emit_recording_state(self):
        bus = EventBus()
        q = bus.subscribe()
        with patch("io_mcp.api.event_bus", bus):
            emit_recording_state("sid-rec", True)
        event = q.get_nowait()
        assert event.event_type == "recording_state"
        assert event.session_id == "sid-rec"
        assert event.data["recording"] is True

    def test_emit_recording_state_off(self):
        bus = EventBus()
        q = bus.subscribe()
        with patch("io_mcp.api.event_bus", bus):
            emit_recording_state("sid-rec", False)
        event = q.get_nowait()
        assert event.data["recording"] is False


# ===========================================================================
# 4. FrontendAPIHandler — HTTP endpoint tests
# ===========================================================================

class TestHealthEndpoint:
    """GET /api/health"""

    def test_health_no_frontend(self, api_server):
        """Health endpoint works even without a frontend attached."""
        srv = api_server()
        status, data = srv.get("/api/health")
        assert status == 200
        assert data["status"] == "ok"
        assert data["sessions"] == 0

    def test_health_with_sessions(self, api_server):
        s1 = _make_session("s1")
        s2 = _make_session("s2")
        frontend = _make_frontend([s1, s2])
        srv = api_server(frontend=frontend)
        status, data = srv.get("/api/health")
        assert status == 200
        assert data["status"] == "ok"
        assert data["sessions"] == 2
        assert "sse_subscribers" in data

    def test_health_sse_subscriber_count(self, api_server):
        """SSE subscriber count reflects actual subscriptions."""
        srv = api_server()
        status, data = srv.get("/api/health")
        assert data["sse_subscribers"] >= 0


class TestSessionsEndpoint:
    """GET /api/sessions"""

    def test_sessions_no_frontend(self, api_server):
        srv = api_server()
        status, data = srv.get("/api/sessions")
        assert status == 500
        assert "error" in data

    def test_sessions_empty(self, api_server):
        frontend = _make_frontend()
        srv = api_server(frontend=frontend)
        status, data = srv.get("/api/sessions")
        assert status == 200
        assert data["sessions"] == []

    def test_sessions_lists_all(self, api_server):
        s1 = _make_session("s1", "Agent 1", active=True, preamble="Pick one",
                           choices=[{"label": "A"}, {"label": "B"}])
        s2 = _make_session("s2", "Agent 2", active=False)
        frontend = _make_frontend([s1, s2])
        srv = api_server(frontend=frontend)
        status, data = srv.get("/api/sessions")
        assert status == 200
        sessions = data["sessions"]
        assert len(sessions) == 2

        # First session (active)
        assert sessions[0]["id"] == "s1"
        assert sessions[0]["name"] == "Agent 1"
        assert sessions[0]["active"] is True
        assert sessions[0]["preamble"] == "Pick one"
        assert len(sessions[0]["choices"]) == 2

        # Second session (inactive — preamble and choices should be empty)
        assert sessions[1]["id"] == "s2"
        assert sessions[1]["active"] is False
        assert sessions[1]["preamble"] == ""
        assert sessions[1]["choices"] == []


class TestSettingsEndpoint:
    """GET /api/settings"""

    def test_settings_no_frontend(self, api_server):
        srv = api_server()
        status, data = srv.get("/api/settings")
        assert status == 500
        assert "error" in data

    def test_settings_no_config(self, api_server):
        frontend = _make_frontend(config=None)
        srv = api_server(frontend=frontend)
        status, data = srv.get("/api/settings")
        assert status == 500
        assert "error" in data

    def test_settings_returns_config(self, api_server):
        config = _make_config(
            tts_model_name="gpt-4o-mini-tts",
            tts_voice="sage",
            tts_speed=1.3,
            tts_emotion="excited",
            stt_model_name="whisper",
        )
        frontend = _make_frontend(config=config)
        srv = api_server(frontend=frontend)
        status, data = srv.get("/api/settings")
        assert status == 200
        assert data["tts_model"] == "gpt-4o-mini-tts"
        assert data["tts_voice"] == "sage"
        assert data["tts_speed"] == 1.3
        assert data["tts_emotion"] == "excited"
        assert data["stt_model"] == "whisper"


class TestSelectEndpoint:
    """POST /api/sessions/:id/select"""

    def test_select_no_frontend(self, api_server):
        srv = api_server()
        status, data = srv.post("/api/sessions/s1/select", {"label": "A", "summary": "First"})
        assert status == 500
        assert "error" in data

    def test_select_unknown_session(self, api_server):
        frontend = _make_frontend()
        srv = api_server(frontend=frontend)
        status, data = srv.post("/api/sessions/nonexistent/select", {"label": "A"})
        assert status == 404
        assert "error" in data

    def test_select_inactive_session(self, api_server):
        s = _make_session("s1", active=False)
        frontend = _make_frontend([s])
        srv = api_server(frontend=frontend)
        status, data = srv.post("/api/sessions/s1/select", {"label": "A"})
        assert status == 404
        assert "not found or inactive" in data["error"]

    def test_select_success(self, api_server):
        s = _make_session("s1", active=True)
        frontend = _make_frontend([s])
        srv = api_server(frontend=frontend)
        status, data = srv.post("/api/sessions/s1/select", {"label": "Go", "summary": "Do it"})
        assert status == 200
        assert data["status"] == "selected"
        assert data["label"] == "Go"
        # Session's selection should be set
        assert s.selection == {"selected": "Go", "summary": "Do it"}
        assert s.selection_event.is_set()

    def test_select_resolves_inbox_item(self, api_server):
        """Select resolves the active inbox item's event."""
        from io_mcp.session import InboxItem

        s = _make_session("s1", active=True)
        item = InboxItem(
            kind="choices",
            preamble="Pick",
            choices=[{"label": "X"}],
        )
        s._active_inbox_item = item
        frontend = _make_frontend([s])
        srv = api_server(frontend=frontend)
        status, data = srv.post("/api/sessions/s1/select", {"label": "X", "summary": "Chosen"})
        assert status == 200
        assert item.done is True
        assert item.result == {"selected": "X", "summary": "Chosen"}
        assert item.event.is_set()

    def test_select_empty_body(self, api_server):
        """Selecting with empty label/summary still works (defaults to "")."""
        s = _make_session("s1", active=True)
        frontend = _make_frontend([s])
        srv = api_server(frontend=frontend)
        status, data = srv.post("/api/sessions/s1/select", {})
        assert status == 200
        assert data["label"] == ""

    def test_select_kicks_drain(self, api_server):
        """Selection should set the drain_kick event."""
        s = _make_session("s1", active=True)
        s.drain_kick.clear()
        frontend = _make_frontend([s])
        srv = api_server(frontend=frontend)
        srv.post("/api/sessions/s1/select", {"label": "Done"})
        assert s.drain_kick.is_set()


class TestMessageEndpoint:
    """POST /api/sessions/:id/message"""

    def test_message_no_frontend(self, api_server):
        srv = api_server()
        status, data = srv.post("/api/sessions/s1/message", {"text": "hello"})
        assert status == 500

    def test_message_unknown_session(self, api_server):
        frontend = _make_frontend()
        srv = api_server(frontend=frontend)
        status, data = srv.post("/api/sessions/nope/message", {"text": "hello"})
        assert status == 404

    def test_message_no_text(self, api_server):
        s = _make_session("s1")
        frontend = _make_frontend([s])
        srv = api_server(frontend=frontend)
        status, data = srv.post("/api/sessions/s1/message", {})
        assert status == 400
        assert "no text" in data["error"]

    def test_message_empty_text(self, api_server):
        s = _make_session("s1")
        frontend = _make_frontend([s])
        srv = api_server(frontend=frontend)
        status, data = srv.post("/api/sessions/s1/message", {"text": ""})
        assert status == 400
        assert "no text" in data["error"]

    def test_message_success(self, api_server):
        s = _make_session("s1")
        frontend = _make_frontend([s])
        srv = api_server(frontend=frontend)
        status, data = srv.post("/api/sessions/s1/message", {"text": "look at this"})
        assert status == 200
        assert data["status"] == "queued"
        assert data["pending"] == 1
        assert s.pending_messages == ["look at this"]

    def test_message_multiple(self, api_server):
        s = _make_session("s1")
        frontend = _make_frontend([s])
        srv = api_server(frontend=frontend)
        srv.post("/api/sessions/s1/message", {"text": "first"})
        status, data = srv.post("/api/sessions/s1/message", {"text": "second"})
        assert data["pending"] == 2
        assert s.pending_messages == ["first", "second"]


class TestBroadcastMessageEndpoint:
    """POST /api/message"""

    def test_broadcast_no_frontend(self, api_server):
        srv = api_server()
        status, data = srv.post("/api/message", {"text": "hello"})
        assert status == 500

    def test_broadcast_no_text(self, api_server):
        s = _make_session("s1")
        frontend = _make_frontend([s])
        srv = api_server(frontend=frontend)
        status, data = srv.post("/api/message", {"text": ""})
        assert status == 400

    def test_broadcast_no_sessions(self, api_server):
        frontend = _make_frontend()
        srv = api_server(frontend=frontend)
        status, data = srv.post("/api/message", {"text": "hello"})
        assert status == 404
        assert "no active sessions" in data["error"]

    def test_broadcast_all(self, api_server):
        s1 = _make_session("s1")
        s2 = _make_session("s2")
        frontend = _make_frontend([s1, s2])
        srv = api_server(frontend=frontend)
        status, data = srv.post("/api/message", {"text": "broadcast msg", "target": "all"})
        assert status == 200
        assert data["count"] == 2
        assert s1.pending_messages == ["broadcast msg"]
        assert s2.pending_messages == ["broadcast msg"]

    def test_broadcast_default_target_is_all(self, api_server):
        s1 = _make_session("s1")
        s2 = _make_session("s2")
        frontend = _make_frontend([s1, s2])
        srv = api_server(frontend=frontend)
        status, data = srv.post("/api/message", {"text": "default target"})
        assert status == 200
        assert data["count"] == 2

    def test_broadcast_active(self, api_server):
        s1 = _make_session("s1")
        s2 = _make_session("s2")
        frontend = _make_frontend([s1, s2], focused=s2)
        srv = api_server(frontend=frontend)
        status, data = srv.post("/api/message", {"text": "active only", "target": "active"})
        assert status == 200
        assert data["count"] == 1
        assert "s2" in data["sessions"]
        assert s1.pending_messages == []
        assert s2.pending_messages == ["active only"]

    def test_broadcast_specific_session(self, api_server):
        s1 = _make_session("s1")
        s2 = _make_session("s2")
        frontend = _make_frontend([s1, s2])
        srv = api_server(frontend=frontend)
        status, data = srv.post("/api/message", {"text": "targeted", "target": "s2"})
        assert status == 200
        assert data["count"] == 1
        assert "s2" in data["sessions"]
        assert s1.pending_messages == []
        assert s2.pending_messages == ["targeted"]

    def test_broadcast_specific_session_not_found(self, api_server):
        s1 = _make_session("s1")
        frontend = _make_frontend([s1])
        srv = api_server(frontend=frontend)
        status, data = srv.post("/api/message", {"text": "hi", "target": "unknown"})
        assert status == 404
        assert "not found" in data["error"]

    def test_broadcast_active_fallback_to_most_recent(self, api_server):
        """When no focused session, 'active' falls back to the most recent."""
        s1 = _make_session("s1")
        s2 = _make_session("s2")
        frontend = _make_frontend([s1, s2])
        # Set active_session_id to None to simulate no focused session
        frontend.manager.active_session_id = None
        srv = api_server(frontend=frontend)
        status, data = srv.post("/api/message", {"text": "fallback", "target": "active"})
        assert status == 200
        assert data["count"] == 1
        # Should fall back to the last session (s2)
        assert s2.pending_messages == ["fallback"]


class TestHighlightEndpoint:
    """POST /api/sessions/:id/highlight"""

    def test_highlight_no_frontend(self, api_server):
        srv = api_server()
        status, data = srv.post("/api/sessions/s1/highlight", {"index": 1})
        assert status == 500

    def test_highlight_unknown_session(self, api_server):
        frontend = _make_frontend()
        srv = api_server(frontend=frontend)
        status, data = srv.post("/api/sessions/nope/highlight", {"index": 1})
        assert status == 404

    def test_highlight_inactive_session(self, api_server):
        s = _make_session("s1", active=False)
        frontend = _make_frontend([s])
        srv = api_server(frontend=frontend)
        status, data = srv.post("/api/sessions/s1/highlight", {"index": 1})
        assert status == 404

    def test_highlight_invalid_index_zero(self, api_server):
        s = _make_session("s1", active=True)
        frontend = _make_frontend([s])
        srv = api_server(frontend=frontend)
        status, data = srv.post("/api/sessions/s1/highlight", {"index": 0})
        assert status == 400
        assert "invalid index" in data["error"]

    def test_highlight_invalid_index_negative(self, api_server):
        s = _make_session("s1", active=True)
        frontend = _make_frontend([s])
        srv = api_server(frontend=frontend)
        status, data = srv.post("/api/sessions/s1/highlight", {"index": -1})
        assert status == 400

    def test_highlight_no_callback(self, api_server):
        """Highlight succeeds even without a callback registered."""
        s = _make_session("s1", active=True)
        frontend = _make_frontend([s])
        srv = api_server(frontend=frontend)
        status, data = srv.post("/api/sessions/s1/highlight", {"index": 3})
        assert status == 200
        assert data["status"] == "highlighted"
        assert data["index"] == 3

    def test_highlight_calls_callback(self, api_server):
        called_with = {}

        def on_highlight(session_id, index):
            called_with["session_id"] = session_id
            called_with["index"] = index

        s = _make_session("s1", active=True)
        frontend = _make_frontend([s])
        srv = api_server(frontend=frontend, highlight_callback=on_highlight)
        status, data = srv.post("/api/sessions/s1/highlight", {"index": 5})
        assert status == 200
        assert called_with == {"session_id": "s1", "index": 5}

    def test_highlight_callback_error(self, api_server):
        def bad_callback(session_id, index):
            raise RuntimeError("highlight failed")

        s = _make_session("s1", active=True)
        frontend = _make_frontend([s])
        srv = api_server(frontend=frontend, highlight_callback=bad_callback)
        status, data = srv.post("/api/sessions/s1/highlight", {"index": 1})
        assert status == 500
        assert "highlight failed" in data["error"]


class TestKeyEndpoint:
    """POST /api/sessions/:id/key"""

    def test_key_no_callback(self, api_server):
        srv = api_server()
        status, data = srv.post("/api/sessions/s1/key", {"key": "j"})
        assert status == 500
        assert "no key handler" in data["error"]

    def test_key_unsupported(self, api_server):
        srv = api_server(key_callback=lambda sid, k: None)
        status, data = srv.post("/api/sessions/s1/key", {"key": "z"})
        assert status == 400
        assert "unsupported key" in data["error"]

    def test_key_empty(self, api_server):
        srv = api_server(key_callback=lambda sid, k: None)
        status, data = srv.post("/api/sessions/s1/key", {"key": ""})
        assert status == 400

    def test_key_all_supported_keys(self, api_server):
        """All supported keys should succeed."""
        received = []

        def on_key(session_id, key):
            received.append((session_id, key))

        srv = api_server(key_callback=on_key)
        supported = ["j", "k", "enter", "space", "u", "h", "l", "s", "d", "n", "m", "i"]
        for key in supported:
            status, data = srv.post("/api/sessions/any/key", {"key": key})
            assert status == 200, f"Key '{key}' should be supported, got {status}"
            assert data["status"] == "ok"
            assert data["key"] == key

        # All keys should have been forwarded
        assert len(received) == len(supported)
        for (sid, k), expected_key in zip(received, supported):
            assert sid == "any"
            assert k == expected_key

    def test_key_callback_error(self, api_server):
        def bad_callback(session_id, key):
            raise ValueError("key handler crashed")

        srv = api_server(key_callback=bad_callback)
        status, data = srv.post("/api/sessions/s1/key", {"key": "j"})
        assert status == 500
        assert "key handler crashed" in data["error"]


class TestSSEEndpoint:
    """GET /api/events — Server-Sent Events stream."""

    def test_sse_stream(self, api_server):
        """SSE stream sends 'connected' event, correct content-type, CORS, and caching headers."""
        srv = api_server()
        status, chunk, headers = srv.get_sse_stream()
        # Status and connected event
        assert status == 200
        assert "event: connected" in chunk
        assert "data: {}" in chunk
        # Content-Type
        assert headers.get("content-type") == "text/event-stream"
        # CORS
        assert headers.get("access-control-allow-origin") == "*"
        # Cache control
        assert "no-cache" in headers.get("cache-control", "")


class TestCORSPreflight:
    """OPTIONS requests for CORS preflight."""

    def test_options_returns_204(self, api_server):
        srv = api_server()
        status, headers = srv.options("/api/events")
        assert status == 204

    def test_options_cors_headers(self, api_server):
        srv = api_server()
        status, headers = srv.options("/api/sessions/s1/select")
        assert headers.get("access-control-allow-origin") == "*"
        assert "POST" in headers.get("access-control-allow-methods", "")
        assert "GET" in headers.get("access-control-allow-methods", "")
        assert "Content-Type" in headers.get("access-control-allow-headers", "")


# ===========================================================================
# 5. Error cases — 404, 400, etc.
# ===========================================================================

class TestErrorCases:
    """Test error responses for invalid paths and requests."""

    def test_get_unknown_path_returns_404(self, api_server):
        srv = api_server()
        status, data = srv.get("/api/nonexistent")
        assert status == 404
        assert data.get("error") == "not found"

    def test_get_root_returns_404(self, api_server):
        srv = api_server()
        status, data = srv.get("/")
        assert status == 404

    def test_post_unknown_path_returns_404(self, api_server):
        srv = api_server()
        status, data = srv.post("/api/nonexistent", {"data": "test"})
        assert status == 404

    def test_post_sessions_invalid_action(self, api_server):
        srv = api_server()
        status, data = srv.post("/api/sessions/s1/invalid_action", {})
        assert status == 404

    def test_json_response_has_cors_header(self, api_server):
        """Even error responses should include CORS headers."""
        srv = api_server()
        conn = http.client.HTTPConnection("127.0.0.1", srv.port)
        conn.request("GET", "/api/nonexistent")
        resp = conn.getresponse()
        resp.read()
        # Check CORS header via raw headers
        cors = dict(resp.getheaders()).get("Access-Control-Allow-Origin")
        assert cors == "*"

    def test_post_no_content_length(self, api_server):
        """POST with no body should still work (reads empty body)."""
        s = _make_session("s1", active=True)
        frontend = _make_frontend([s])
        srv = api_server(frontend=frontend)
        conn = http.client.HTTPConnection("127.0.0.1", srv.port)
        conn.request("POST", "/api/sessions/s1/select", body="",
                     headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        data = json.loads(resp.read().decode())
        # Empty body → label and summary default to ""
        assert resp.status == 200

    def test_post_malformed_json(self, api_server):
        """Malformed JSON body should be handled gracefully."""
        s = _make_session("s1", active=True)
        frontend = _make_frontend([s])
        srv = api_server(frontend=frontend)
        conn = http.client.HTTPConnection("127.0.0.1", srv.port)
        body = b"this is not json"
        conn.request("POST", "/api/sessions/s1/select", body=body,
                     headers={"Content-Type": "application/json",
                              "Content-Length": str(len(body))})
        resp = conn.getresponse()
        # _read_body returns {} for invalid JSON, so label/summary default to ""
        assert resp.status == 200


# ===========================================================================
# 6. start_api_server integration
# ===========================================================================

class TestStartAPIServer:
    """Test the start_api_server convenience function."""

    def test_starts_and_responds(self):
        port = _free_port()
        s = _make_session("s1")
        frontend = _make_frontend([s], config=_make_config())
        thread = start_api_server(frontend, port=port, host="127.0.0.1")
        assert _wait_for_port("127.0.0.1", port)

        # Health endpoint should work
        conn = http.client.HTTPConnection("127.0.0.1", port)
        conn.request("GET", "/api/health")
        resp = conn.getresponse()
        data = json.loads(resp.read().decode())
        assert resp.status == 200
        assert data["status"] == "ok"

    def test_passes_highlight_callback(self):
        port = _free_port()
        called = []

        def on_highlight(sid, idx):
            called.append((sid, idx))

        s = _make_session("s1", active=True)
        frontend = _make_frontend([s])
        start_api_server(frontend, port=port, host="127.0.0.1",
                         highlight_callback=on_highlight)
        assert _wait_for_port("127.0.0.1", port)

        conn = http.client.HTTPConnection("127.0.0.1", port)
        body = json.dumps({"index": 2})
        conn.request("POST", "/api/sessions/s1/highlight", body=body,
                     headers={"Content-Type": "application/json",
                              "Content-Length": str(len(body))})
        resp = conn.getresponse()
        assert resp.status == 200
        assert called == [("s1", 2)]

    def test_passes_key_callback(self):
        port = _free_port()
        called = []

        def on_key(sid, key):
            called.append((sid, key))

        frontend = _make_frontend()
        start_api_server(frontend, port=port, host="127.0.0.1",
                         key_callback=on_key)
        assert _wait_for_port("127.0.0.1", port)

        conn = http.client.HTTPConnection("127.0.0.1", port)
        body = json.dumps({"key": "enter"})
        conn.request("POST", "/api/sessions/s1/key", body=body,
                     headers={"Content-Type": "application/json",
                              "Content-Length": str(len(body))})
        resp = conn.getresponse()
        assert resp.status == 200
        assert called == [("s1", "enter")]


# ===========================================================================
# 7. Edge cases and integration
# ===========================================================================

class TestEdgeCases:
    """Additional edge cases for completeness."""

    def test_session_id_extraction_from_path(self, api_server):
        """Session ID is extracted correctly from nested path segments."""
        s = _make_session("abc-123-def")
        s.active = True
        frontend = _make_frontend([s])
        srv = api_server(frontend=frontend)
        status, data = srv.post("/api/sessions/abc-123-def/select",
                                {"label": "X", "summary": "Y"})
        assert status == 200
        assert data["label"] == "X"

    def test_concurrent_requests(self, api_server):
        """Multiple concurrent requests don't crash the server."""
        s = _make_session("s1")
        frontend = _make_frontend([s], config=_make_config())
        srv = api_server(frontend=frontend)

        results = []
        errors = []

        def worker():
            try:
                status, data = srv.get("/api/health")
                results.append(status)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert not errors, f"Concurrent requests caused errors: {errors}"
        assert all(s == 200 for s in results)

    def test_read_body_zero_length(self, api_server):
        """POST with Content-Length: 0 returns empty dict body."""
        s = _make_session("s1", active=True)
        frontend = _make_frontend([s])
        srv = api_server(frontend=frontend)
        conn = http.client.HTTPConnection("127.0.0.1", srv.port)
        conn.request("POST", "/api/sessions/s1/select",
                     headers={"Content-Type": "application/json",
                              "Content-Length": "0"})
        resp = conn.getresponse()
        data = json.loads(resp.read().decode())
        # Empty body → defaults
        assert resp.status == 200
        assert data["label"] == ""

    def test_highlight_missing_index_field(self, api_server):
        """Highlight with no index field defaults to -1, which is invalid."""
        s = _make_session("s1", active=True)
        frontend = _make_frontend([s])
        srv = api_server(frontend=frontend)
        status, data = srv.post("/api/sessions/s1/highlight", {})
        assert status == 400
        assert "invalid index" in data["error"]

    def test_key_missing_key_field(self, api_server):
        """Key endpoint with no 'key' field defaults to empty string → unsupported."""
        srv = api_server(key_callback=lambda sid, k: None)
        status, data = srv.post("/api/sessions/s1/key", {})
        assert status == 400
        assert "unsupported key" in data["error"]

    def test_multiple_select_overwrites(self, api_server):
        """Multiple selects overwrite the session's selection."""
        s = _make_session("s1", active=True)
        frontend = _make_frontend([s])
        srv = api_server(frontend=frontend)
        srv.post("/api/sessions/s1/select", {"label": "First"})
        srv.post("/api/sessions/s1/select", {"label": "Second"})
        assert s.selection["selected"] == "Second"
