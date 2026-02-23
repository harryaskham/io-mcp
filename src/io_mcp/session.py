"""Per-session state for multi-agent io-mcp.

Each MCP client (streamable-http connection) gets a Session object that holds
its own choices, selection event, speech inbox, and UI state.
SessionManager handles routing between sessions and tab navigation.
"""

from __future__ import annotations

import os
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
    last_tool_name: str = ""                 # name of the last MCP tool called
    tool_call_count: int = 0                 # total number of tool calls made
    heartbeat_spoken: bool = False
    ambient_count: int = 0                  # how many ambient updates spoken this silence period

    # ── Selection history ─────────────────────────────────────────
    history: list[HistoryEntry] = field(default_factory=list)

    # ── Undo support ──────────────────────────────────────────────
    last_preamble: str = ""                  # previous present_choices preamble
    last_choices: list[dict] = field(default_factory=list)  # previous choices

    # ── User message inbox (queued for next MCP response) ─────────
    pending_messages: list[str] = field(default_factory=list)

    # ── Agent health monitoring ───────────────────────────────────
    health_status: str = "healthy"           # "healthy", "warning", "unresponsive"
    health_alert_spoken: bool = False        # True once we've spoken the warning alert
    health_last_check: float = 0.0          # timestamp of last health evaluation

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

    def restore_activity(self, data: dict) -> None:
        """Restore persisted activity data onto this session.

        Called when an agent re-registers after a restart. Restores
        speech log, history, and tool stats from the persisted data
        so dashboard/timeline show continuous history.

        Args:
            data: Dict from load_registered() with speech_log, history,
                  tool_call_count, last_tool_name, last_tool_call keys.
        """
        # Restore speech log
        saved_speech = data.get("speech_log", [])
        if saved_speech:
            for entry in saved_speech:
                self.speech_log.append(SpeechEntry(
                    text=entry.get("text", ""),
                    timestamp=entry.get("timestamp", time.time()),
                    played=True,  # don't replay old speech
                ))

        # Restore history
        saved_history = data.get("history", [])
        if saved_history:
            for entry in saved_history:
                self.history.append(HistoryEntry(
                    label=entry.get("label", ""),
                    summary=entry.get("summary", ""),
                    preamble=entry.get("preamble", ""),
                    timestamp=entry.get("timestamp", time.time()),
                ))

        # Restore tool stats
        self.tool_call_count = data.get("tool_call_count", 0)
        self.last_tool_name = data.get("last_tool_name", "")
        saved_last_call = data.get("last_tool_call", 0)
        if saved_last_call > 0:
            self.last_tool_call = saved_last_call

    def summary(self) -> str:
        """Build a concise activity summary for this session.

        Returns a short string describing what the agent has been doing,
        suitable for display in the dashboard or narration via TTS.

        Example outputs:
            "12 tool calls, 3 selections. Last: speak_async. Working for 5m."
            "Waiting for user selection. 8 tool calls."
            "Just connected, no activity yet."
        """
        now = time.time()

        # Tool call stats
        count = self.tool_call_count
        if count == 0:
            return "Just connected, no activity yet."

        # Selections made
        n_selections = len(self.history)

        # Health/status info
        if self.active:
            status = "Waiting for user selection"
        elif self.health_status == "unresponsive":
            status = "Unresponsive"
        elif self.health_status == "warning":
            status = "May be stuck"
        else:
            status = "Working"

        # Elapsed time since connection
        elapsed = now - (self.last_activity - (now - self.last_tool_call) if self.last_tool_call else now)
        # More useful: time since first tool call
        # Use creation time (approximated by last_activity minus elapsed)
        session_age = now - (self.last_tool_call - (self.tool_call_count * 2))  # rough estimate
        if self.history:
            first_action = min(h.timestamp for h in self.history)
            session_age = now - first_action

        # Format duration
        mins = int(session_age) // 60
        if mins > 60:
            age_str = f"{mins // 60}h{mins % 60:02d}m"
        elif mins > 0:
            age_str = f"{mins}m"
        else:
            age_str = f"{int(session_age)}s"

        # Build parts
        parts = [status]
        parts.append(f"{count} tool call{'s' if count != 1 else ''}")
        if n_selections > 0:
            parts.append(f"{n_selections} selection{'s' if n_selections != 1 else ''}")
        if self.last_tool_name:
            parts.append(f"last: {self.last_tool_name}")
        parts.append(f"up {age_str}")

        return ". ".join(parts) + "."

    def timeline(self, max_entries: int = 20) -> list[dict]:
        """Build a chronological timeline of session activity.

        Merges speech entries and history (selections) into a unified
        timeline sorted by timestamp. Each entry has:
            type:      "speech" | "selection"
            text:      The speech text or selection label
            detail:    Summary for selections, empty for speech
            timestamp: Unix timestamp
            age:       Human-readable age string ("2m ago", "1h30m ago")

        Args:
            max_entries: Maximum number of entries to return (most recent).

        Returns:
            List of timeline entry dicts, most recent first.
        """
        now = time.time()
        entries: list[dict] = []

        # Add speech entries
        for s in self.speech_log:
            entries.append({
                "type": "speech",
                "text": s.text,
                "detail": "",
                "timestamp": s.timestamp,
            })

        # Add history (selection) entries
        for h in self.history:
            entries.append({
                "type": "selection",
                "text": h.label,
                "detail": h.summary,
                "timestamp": h.timestamp,
            })

        # Sort by timestamp descending (most recent first)
        entries.sort(key=lambda e: e["timestamp"], reverse=True)

        # Trim to max
        entries = entries[:max_entries]

        # Add age strings
        for e in entries:
            age_secs = now - e["timestamp"]
            if age_secs < 60:
                e["age"] = f"{int(age_secs)}s ago"
            elif age_secs < 3600:
                mins = int(age_secs) // 60
                e["age"] = f"{mins}m ago"
            else:
                hours = int(age_secs) // 3600
                mins = (int(age_secs) % 3600) // 60
                e["age"] = f"{hours}h{mins:02d}m ago"

        return entries


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

    def tab_bar_text(self, accent: str = "#88c0d0", success: str = "#a3be8c",
                     warning: str = "#ebcb8b", error: str = "#bf616a") -> str:
        """Render the tab bar string with rich formatting.

        Active tab is highlighted with brackets and bold.
        Tabs with pending choices get a dot indicator.
        Tabs with unhealthy agents get a health indicator (warning=yellow, unresponsive=red).
        Colors are passed from the TUI's active color scheme.
        """
        with self._lock:
            if not self.session_order:
                return ""
            parts = []
            for sid in self.session_order:
                session = self.sessions[sid]
                name = session.name
                # Choice indicator
                indicator = f" [bold {success}]●[/bold {success}]" if session.active else ""
                # Health indicator (only when not showing active choices)
                health = getattr(session, 'health_status', 'healthy')
                if not session.active:
                    if health == "warning":
                        indicator = f" [bold {warning}]⚠[/bold {warning}]"
                    elif health == "unresponsive":
                        indicator = f" [bold {error}]✗[/bold {error}]"
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

    # ─── Session persistence ──────────────────────────────────────

    PERSIST_FILE = os.path.join(
        os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state")),
        "io-mcp", "sessions.json",
    )

    def save_registered(self) -> None:
        """Persist registered session metadata and activity to disk.

        Saves sessions that have called register_session(), including:
        - Registration metadata (name, cwd, hostname, tmux info)
        - Speech log (last 100 entries: text + timestamp)
        - Selection history (all entries: label, summary, preamble, timestamp)
        - Tool call stats (count, last tool name)
        - TTS overrides (voice, emotion)
        """
        import json

        registered = []
        with self._lock:
            for sid in self.session_order:
                session = self.sessions.get(sid)
                if session and session.registered:
                    # Speech log: save last 100 entries (text + timestamp only)
                    speech = [
                        {"text": s.text, "timestamp": s.timestamp}
                        for s in (session.speech_log or [])[-100:]
                    ]

                    # History: save all entries
                    history = [
                        {
                            "label": h.label,
                            "summary": h.summary,
                            "preamble": h.preamble,
                            "timestamp": h.timestamp,
                        }
                        for h in (session.history or [])
                    ]

                    registered.append({
                        "name": session.name,
                        "cwd": session.cwd,
                        "hostname": session.hostname,
                        "tmux_session": session.tmux_session,
                        "tmux_pane": session.tmux_pane,
                        "voice_override": session.voice_override,
                        "emotion_override": session.emotion_override,
                        "agent_metadata": session.agent_metadata,
                        # Activity data
                        "speech_log": speech,
                        "history": history,
                        "tool_call_count": session.tool_call_count,
                        "last_tool_name": session.last_tool_name,
                        "last_tool_call": session.last_tool_call,
                    })

        try:
            persist_dir = os.path.dirname(self.PERSIST_FILE)
            os.makedirs(persist_dir, exist_ok=True)
            with open(self.PERSIST_FILE, "w") as f:
                json.dump({"sessions": registered}, f, indent=2)
        except Exception:
            pass

    def load_registered(self) -> list[dict]:
        """Load persisted session metadata and activity from disk.

        Returns list of session metadata dicts including speech log,
        history, and tool stats. Does NOT create sessions — that happens
        when agents reconnect and re-register.

        Each dict contains:
            name, cwd, hostname, tmux_session, tmux_pane,
            voice_override, emotion_override, agent_metadata,
            speech_log (list of {text, timestamp}),
            history (list of {label, summary, preamble, timestamp}),
            tool_call_count, last_tool_name, last_tool_call
        """
        import json

        try:
            with open(self.PERSIST_FILE, "r") as f:
                data = json.load(f)
            return data.get("sessions", [])
        except (FileNotFoundError, json.JSONDecodeError):
            return []
