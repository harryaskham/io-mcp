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
