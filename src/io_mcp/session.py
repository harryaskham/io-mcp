"""Per-session state for multi-agent io-mcp.

Each MCP client (streamable-http connection) gets a Session object that holds
its own choices, selection event, speech inbox, and UI state.
SessionManager handles routing between sessions and tab navigation.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class SpeechEntry:
    """A single speech event in a session's inbox."""
    text: str
    timestamp: float = field(default_factory=time.time)
    played: bool = False
    priority: int = 0  # 0=normal, 1=urgent (interrupts current playback)


@dataclass
class HistoryEntry:
    """A recorded selection from present_choices."""
    label: str
    summary: str
    preamble: str
    timestamp: float = field(default_factory=time.time)


@dataclass
class Session:
    """State for one MCP client session (one tab)."""

    session_id: str                          # MCP session ID (UUID for streamable-http)
    name: str                                # "Agent 1", "Agent 2", ...

    # ── Choice state ──────────────────────────────────────────────
    preamble: str = ""
    choices: list[dict] = field(default_factory=list)
    all_items: list[dict] = field(default_factory=list)
    extras_count: int = 0
    selection: Optional[dict] = None
    selection_event: threading.Event = field(default_factory=threading.Event)
    active: bool = False                     # has pending present_choices()
    intro_speaking: bool = False
    reading_options: bool = False

    # ── Speech inbox ──────────────────────────────────────────────
    speech_log: list[SpeechEntry] = field(default_factory=list)
    unplayed_speech: list[SpeechEntry] = field(default_factory=list)

    # ── UI state (saved/restored on tab switch) ───────────────────
    scroll_index: int = 0                    # remembered cursor position in choices list

    # ── Input modes (per-session) ─────────────────────────────────
    input_mode: bool = False
    voice_recording: bool = False
    in_settings: bool = False

    # ── Per-session TTS overrides (for voice/emotion rotation) ────
    voice_override: Optional[str] = None
    emotion_override: Optional[str] = None

    # ── Activity tracking (for auto-cleanup) ──────────────────────
    last_activity: float = field(default_factory=time.time)
    last_tool_call: float = field(default_factory=time.time)
    heartbeat_spoken: bool = False
    ambient_count: int = 0                  # how many ambient updates spoken this silence period

    # ── Selection history ─────────────────────────────────────────
    history: list[HistoryEntry] = field(default_factory=list)

    # ── Undo support ──────────────────────────────────────────────
    last_preamble: str = ""                  # previous present_choices preamble
    last_choices: list[dict] = field(default_factory=list)  # previous choices

    # ── User message inbox (queued for next MCP response) ─────────
    pending_messages: list[str] = field(default_factory=list)

    # ── Agent registration metadata ─────────────────────────────
    registered: bool = False                 # has the agent called register_session?
    cwd: str = ""                            # agent's working directory
    hostname: str = ""                       # machine the agent is running on
    tmux_session: str = ""                   # tmux session name (if any)
    tmux_pane: str = ""                      # tmux pane ID (e.g. %42)
    agent_metadata: dict = field(default_factory=dict)  # arbitrary extra metadata

    def touch(self) -> None:
        """Update the last_activity timestamp."""
        self.last_activity = time.time()

    def drain_messages(self) -> str:
        """Drain and return all pending user messages as a formatted string.

        Returns empty string if no messages queued. Otherwise returns
        a block that can be appended to MCP tool responses.
        """
        msgs = getattr(self, 'pending_messages', [])
        if not msgs:
            return ""
        drained = list(msgs)
        msgs.clear()
        lines = "\n".join(f"- {m}" for m in drained)
        return f"\n\n--- Queued User Messages ---\n{lines}"


class SessionManager:
    """Manages multiple sessions with tab navigation.

    Thread-safe — all mutations go through the lock.
    """

    def __init__(self) -> None:
        self.sessions: dict[str, Session] = {}
        self.session_order: list[str] = []      # ordered list of session IDs
        self.active_session_id: Optional[str] = None
        self._counter: int = 0
        self._lock = threading.Lock()

    def get_or_create(self, session_id: str) -> tuple[Session, bool]:
        """Get existing session or create a new one.

        Returns (session, created) where created is True if new.
        """
        with self._lock:
            if session_id in self.sessions:
                return self.sessions[session_id], False

            self._counter += 1
            name = f"Agent {self._counter}"
            session = Session(session_id=session_id, name=name)
            self.sessions[session_id] = session
            self.session_order.append(session_id)

            # Auto-focus first session
            if self.active_session_id is None:
                self.active_session_id = session_id

            return session, True

    def remove(self, session_id: str) -> Optional[str]:
        """Remove a session. Returns new active_session_id (or None).

        If the removed session was focused, focuses the next available.
        """
        with self._lock:
            if session_id not in self.sessions:
                return self.active_session_id

            del self.sessions[session_id]
            self.session_order.remove(session_id)

            if self.active_session_id == session_id:
                if self.session_order:
                    self.active_session_id = self.session_order[0]
                else:
                    self.active_session_id = None

            return self.active_session_id

    def focused(self) -> Optional[Session]:
        """Get the currently focused session."""
        with self._lock:
            if self.active_session_id is None:
                return None
            return self.sessions.get(self.active_session_id)

    def focus(self, session_id: str) -> Optional[Session]:
        """Set focus to a specific session. Returns the session."""
        with self._lock:
            if session_id not in self.sessions:
                return None
            self.active_session_id = session_id
            return self.sessions[session_id]

    def next_tab(self) -> Optional[Session]:
        """Move focus to the next tab. Returns new focused session."""
        with self._lock:
            if not self.session_order or self.active_session_id is None:
                return None
            idx = self.session_order.index(self.active_session_id)
            idx = (idx + 1) % len(self.session_order)
            self.active_session_id = self.session_order[idx]
            return self.sessions[self.active_session_id]

    def prev_tab(self) -> Optional[Session]:
        """Move focus to the previous tab. Returns new focused session."""
        with self._lock:
            if not self.session_order or self.active_session_id is None:
                return None
            idx = self.session_order.index(self.active_session_id)
            idx = (idx - 1) % len(self.session_order)
            self.active_session_id = self.session_order[idx]
            return self.sessions[self.active_session_id]

    def next_with_choices(self) -> Optional[Session]:
        """Cycle to the next tab that has active choices. Returns session or None."""
        with self._lock:
            if not self.session_order or self.active_session_id is None:
                return None

            start_idx = self.session_order.index(self.active_session_id)
            n = len(self.session_order)

            for offset in range(1, n + 1):
                idx = (start_idx + offset) % n
                sid = self.session_order[idx]
                session = self.sessions[sid]
                if session.active:
                    self.active_session_id = sid
                    return session

            return None  # no other session has active choices

    def count(self) -> int:
        """Number of active sessions."""
        with self._lock:
            return len(self.sessions)

    def all_sessions(self) -> list[Session]:
        """All sessions in tab order (snapshot)."""
        with self._lock:
            return [self.sessions[sid] for sid in self.session_order if sid in self.sessions]

    def get(self, session_id: str) -> Optional[Session]:
        """Get a session by ID."""
        with self._lock:
            return self.sessions.get(session_id)

    def tab_bar_text(self, accent: str = "#88c0d0", success: str = "#a3be8c") -> str:
        """Render the tab bar string with rich formatting.

        Active tab is highlighted with brackets and bold.
        Tabs with pending choices get a dot indicator.
        Colors are passed from the TUI's active color scheme.
        """
        with self._lock:
            if not self.session_order:
                return ""
            parts = []
            for sid in self.session_order:
                session = self.sessions[sid]
                name = session.name
                indicator = f" [bold {success}]●[/bold {success}]" if session.active else ""
                if sid == self.active_session_id:
                    parts.append(f"[bold {accent}]▸ {name}[/bold {accent}]{indicator}")
                else:
                    parts.append(f"[dim]  {name}[/dim]{indicator}")
            return "  ".join(parts)

    def cleanup_stale(self, timeout_seconds: float = 300.0) -> list[str]:
        """Remove sessions that have been inactive for longer than timeout.

        A session is considered stale if:
        - It is NOT the currently focused session
        - It does NOT have active choices (pending present_choices)
        - Its last_activity is older than timeout_seconds ago

        Returns a list of removed session IDs.
        """
        now = time.time()
        to_remove: list[str] = []

        with self._lock:
            for sid in list(self.session_order):
                session = self.sessions.get(sid)
                if session is None:
                    continue
                # Never remove the focused session
                if sid == self.active_session_id:
                    continue
                # Never remove sessions with active choices
                if session.active:
                    continue
                # Check if stale
                activity = getattr(session, 'last_activity', now)
                if now - activity > timeout_seconds:
                    to_remove.append(sid)

        # Remove outside the lock (remove() acquires its own lock)
        for sid in to_remove:
            self.remove(sid)

        return to_remove
