"""Per-session state for multi-agent io-mcp.

Each MCP client (streamable-http connection) gets a Session object that holds
its own choices, selection event, speech inbox, and UI state.
SessionManager handles routing between sessions and tab navigation.

Inbox model: each session has a queue of InboxItem objects. Multiple
present_choices/speak calls can be queued without clobbering each other.
The TUI drains the queue in order — showing one choice set at a time,
playing speech in sequence.
"""

from __future__ import annotations

import collections
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
class InboxItem:
    """A queued tool call waiting for TUI display/response.

    Each present_choices call creates one InboxItem with its own
    threading.Event, so the calling thread can block independently.
    Speech calls also create InboxItems but resolve immediately
    after playback.
    """
    kind: str  # "choices" or "speech"
    # Choices fields
    preamble: str = ""
    choices: list[dict] = field(default_factory=list)
    # Speech fields
    text: str = ""
    blocking: bool = False
    priority: int = 0
    # Resolution
    result: Optional[dict] = None
    event: threading.Event = field(default_factory=threading.Event)
    timestamp: float = field(default_factory=time.time)
    done: bool = False
    # Processing guard — prevents multiple drain workers from activating the same item
    processing: bool = False
    # Thread tracking — used to detect orphaned items when the HTTP thread dies
    owner_thread: Optional[threading.Thread] = field(default_factory=lambda: threading.current_thread())


@dataclass
class Session:
    """State for one MCP client session (one tab)."""

    session_id: str                          # MCP session ID (UUID for streamable-http)
    name: str                                # "Agent 1", "Agent 2", ...

    # ── Inbox concurrency control ───────────────────────────────────
    # Guards dedup-check + enqueue so concurrent threads can't both
    # pass the duplicate check before either has enqueued.
    _inbox_lock: threading.Lock = field(default_factory=threading.Lock)

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
    inbox_pane_focused: bool = False          # was inbox pane focused when we switched away?

    # ── Input modes (per-session) ─────────────────────────────────
    input_mode: bool = False
    voice_recording: bool = False
    in_settings: bool = False

    # ── Per-session TTS overrides (for voice/emotion rotation) ────
    voice_override: Optional[str] = None
    model_override: Optional[str] = None       # TTS model override (for voice rotation across providers)
    emotion_override: Optional[str] = None

    # ── Activity tracking (for auto-cleanup) ──────────────────────
    last_activity: float = field(default_factory=time.time)
    last_tool_call: float = field(default_factory=time.time)
    last_tool_name: str = ""                 # name of the last MCP tool called
    tool_call_count: int = 0                 # total number of tool calls made
    heartbeat_spoken: bool = False
    ambient_count: int = 0                  # how many ambient updates spoken this silence period

    # ── Activity log (timestamped feed of agent actions) ──────────
    activity_log: list[dict] = field(default_factory=list)
    _activity_log_max: int = 50  # cap to prevent unbounded growth

    # ── Achievements ──────────────────────────────────────────────
    achievements_unlocked: set = field(default_factory=set)

    @property
    def streak_minutes(self) -> int:
        """Consecutive minutes of activity. Resets after 2min idle."""
        if not self.activity_log:
            return 0
        now = time.time()
        # Check if currently idle (gap > 120s since last activity)
        if now - self.activity_log[-1]["timestamp"] > 120:
            return 0
        # Walk backward to find the start of the streak
        streak_start = self.activity_log[-1]["timestamp"]
        for i in range(len(self.activity_log) - 1, 0, -1):
            gap = self.activity_log[i]["timestamp"] - self.activity_log[i - 1]["timestamp"]
            if gap > 120:  # 2 minute gap breaks the streak
                break
            streak_start = self.activity_log[i - 1]["timestamp"]
        return max(1, int((now - streak_start) / 60))

    # ── Selection history ─────────────────────────────────────────
    history: list[HistoryEntry] = field(default_factory=list)

    # ── Undo support ──────────────────────────────────────────────
    last_preamble: str = ""                  # previous present_choices preamble
    last_choices: list[dict] = field(default_factory=list)  # previous choices

    # ── User message inbox (queued for next MCP response) ─────────
    pending_messages: list[str] = field(default_factory=list)

    # ── Tool call inbox (queued choices/speech for TUI display) ──
    inbox: collections.deque = field(default_factory=collections.deque)
    inbox_done: list[InboxItem] = field(default_factory=list)
    _inbox_done_max: int = 50  # cap to prevent unbounded growth
    # Generation counter — bumped on every inbox mutation so the TUI can
    # skip redundant _update_inbox_list() rebuilds.
    _inbox_generation: int = 0
    # Kicked after resolving an inbox item so waiting threads wake immediately
    drain_kick: threading.Event = field(default_factory=threading.Event)

    # ── Agent health monitoring ───────────────────────────────────
    health_status: str = "healthy"           # "healthy", "warning", "unresponsive"
    health_alert_spoken: bool = False        # True once we've spoken the warning alert
    health_last_check: float = 0.0          # timestamp of last health evaluation

    # ── Agent registration metadata ─────────────────────────────
    registered: bool = False                 # has the agent called register_session?
    cwd: str = ""                            # agent's working directory
    hostname: str = ""                       # machine the agent is running on
    username: str = ""                       # user running the agent
    tmux_session: str = ""                   # tmux session name (if any)
    tmux_pane: str = ""                      # tmux pane ID (e.g. %42)
    agent_metadata: dict = field(default_factory=dict)  # arbitrary extra metadata

    def touch(self) -> None:
        """Update the last_activity timestamp."""
        self.last_activity = time.time()

    @property
    def mood(self) -> str:
        """Compute agent mood from recent activity.

        Returns a mood string used to tint the TUI preamble:
        - "idle"      — no activity in last 30s (calm blue)
        - "flowing"   — steady tool calls, 1-3 per 10s (green)
        - "busy"      — rapid tool calls, 4-8 per 10s (yellow)
        - "thrashing" — very rapid, 9+ per 10s (red)
        - "speaking"  — last action was speech (purple)
        """
        if not self.activity_log:
            return "idle"

        now = time.time()
        last = self.activity_log[-1]

        # If last activity was speech in last 10s → speaking
        if last["kind"] == "speech" and (now - last["timestamp"]) < 10:
            return "speaking"

        # Count activities in last 10 seconds
        recent = sum(1 for e in self.activity_log if now - e["timestamp"] < 10)

        # Check if idle (nothing in 30s)
        if now - last["timestamp"] > 30:
            return "idle"

        if recent >= 9:
            return "thrashing"
        elif recent >= 4:
            return "busy"
        elif recent >= 1:
            return "flowing"
        return "idle"

    def check_achievements(self) -> list[str]:
        """Check for newly unlocked achievements. Returns list of new ones.

        Called after each tool call. Each achievement fires only once.
        """
        new = []
        now = time.time()

        checks = [
            ("first_blood", self.tool_call_count >= 1,
             "🏆 First Blood — first tool call!"),
            ("getting_started", self.tool_call_count >= 10,
             "🎯 Getting Started — 10 tool calls"),
            ("centurion", self.tool_call_count >= 100,
             "💯 Centurion — 100 tool calls!"),
            ("five_hundred", self.tool_call_count >= 500,
             "🔥 Unstoppable — 500 tool calls!"),
            ("chatterbox", len(self.speech_log) >= 20,
             "💬 Chatterbox — 20 speech events"),
            ("decisive", len(self.history) >= 5,
             "⚡ Decisive — 5 selections made"),
            ("veteran", len(self.history) >= 20,
             "🎖️ Veteran — 20 selections made"),
        ]

        # Time-based achievements
        if self.history:
            first = min(h.timestamp for h in self.history)
            session_mins = (now - first) / 60
            checks.append(("marathon", session_mins >= 30,
                          "🏃 Marathon — 30 minutes of work"))
            checks.append(("ultra", session_mins >= 60,
                          "🦾 Ultra — 1 hour session!"))

        # Night owl: any activity between midnight and 5am
        hour = time.localtime(now).tm_hour
        checks.append(("night_owl", hour < 5,
                       "🦉 Night Owl — coding past midnight"))

        # Speed demon: 5+ tool calls in last 5 seconds
        recent_5s = sum(1 for e in self.activity_log if now - e["timestamp"] < 5)
        checks.append(("speed_demon", recent_5s >= 5,
                       "⚡ Speed Demon — 5 actions in 5 seconds"))

        for key, condition, message in checks:
            if condition and key not in self.achievements_unlocked:
                self.achievements_unlocked.add(key)
                new.append(message)

        return new

    def log_activity(self, tool: str, detail: str = "", kind: str = "tool") -> None:
        """Append a timestamped entry to the activity log.

        Args:
            tool: Tool name or action identifier.
            detail: Short description or preview text.
            kind: Event type — "tool", "speech", "selection", "status".
        """
        entry = {
            "timestamp": time.time(),
            "tool": tool,
            "detail": detail,
            "kind": kind,
        }
        self.activity_log.append(entry)
        # Trim from the front when over the cap
        overflow = len(self.activity_log) - self._activity_log_max
        if overflow > 0:
            self.activity_log = self.activity_log[overflow:]

    def enqueue(self, item: InboxItem) -> None:
        """Add an item to the inbox queue."""
        self.inbox.append(item)
        self._inbox_generation += 1

    def enqueue_speech(self, text: str, blocking: bool = True,
                       priority: int = 0) -> InboxItem:
        """Create and enqueue a speech InboxItem.

        Args:
            text: The speech text.
            blocking: Whether the agent should block until TTS finishes.
            priority: 0=normal, 1=urgent (interrupts current playback).

        Returns:
            The enqueued InboxItem.
        """
        item = InboxItem(
            kind="speech",
            text=text,
            blocking=blocking,
            priority=priority,
            preamble=text,
        )
        if priority >= 1:
            # Urgent: insert at front of inbox
            self.inbox.appendleft(item)
        else:
            self.inbox.append(item)
        self._inbox_generation += 1
        self.drain_kick.set()
        return item

    def dedup_and_enqueue(self, item: InboxItem) -> "bool | InboxItem":
        """Atomically check for duplicates and enqueue a choices item.

        Under the inbox lock:
        1. If a pending (not-done) item with identical preamble+choice labels
           already exists, return it so the caller can piggyback — wait on
           the existing item's event and return its result.  This prevents
           MCP client retries from cancelling/re-creating inbox items.
        2. Otherwise enqueue normally.

        Returns:
            True if the item was enqueued as new.
            An existing InboxItem if the caller should piggyback on it.
            False should not occur (kept for API compat).
        """
        key = (item.preamble, tuple(c.get("label", "") for c in item.choices))

        with self._inbox_lock:
            # ── Piggyback on existing pending item with identical content ──
            for existing in list(self.inbox):
                if existing.done:
                    continue
                existing_key = (
                    existing.preamble,
                    tuple(c.get("label", "") for c in existing.choices),
                )
                if existing_key == key:
                    # Don't cancel the existing item — it's already being
                    # presented (or queued).  The caller should wait on it.
                    return existing

            # ── Enqueue as new ──
            self.inbox.append(item)
            self._inbox_generation += 1

            return True

    def _append_done(self, item: InboxItem) -> None:
        """Move an item to inbox_done, skipping _restart items and capping size.

        Items resolved with ``_restart`` (retries, duplicates, owner-died) are
        not real user selections — they add noise to the inbox history and waste
        memory.  We drop them entirely.

        Also trims ``inbox_done`` to ``_inbox_done_max`` to prevent unbounded
        growth that degrades TUI performance.
        """
        # Skip items that were never really presented to the user
        result = item.result or {}
        if result.get("selected") == "_restart":
            return

        self.inbox_done.append(item)
        self._inbox_generation += 1

        # Trim from the front when over the cap
        overflow = len(self.inbox_done) - self._inbox_done_max
        if overflow > 0:
            del self.inbox_done[:overflow]

    def peek_inbox(self) -> Optional[InboxItem]:
        """Get the next unresolved inbox item without removing it.

        Auto-cleans orphaned items whose owner thread has died (e.g. when
        the HTTP connection was dropped). These are moved to inbox_done
        so the next live item can proceed. Kicks drain_kick when cleaning
        orphans so waiting threads wake immediately.
        """
        kicked = False
        while self.inbox:
            front = self.inbox[0]
            if front.done:
                self._append_done(self.inbox.popleft())
                continue
            # Check if the owner thread died (orphaned item)
            owner = getattr(front, 'owner_thread', None)
            if owner is not None and not owner.is_alive() and not front.done:
                front.done = True
                front.result = {"selected": "_restart", "summary": "Owner thread died"}
                front.event.set()
                self._append_done(self.inbox.popleft())
                kicked = True
                continue
            if kicked:
                self.drain_kick.set()
            return front
        if kicked:
            self.drain_kick.set()
        return None

    def resolve_front(self, result: dict) -> Optional[InboxItem]:
        """Resolve the front inbox item with a result and move it to done.

        Sets the result, marks done, and signals the event so the
        blocking thread can return. Kicks drain_kick so the next
        queued item wakes immediately.
        """
        item = self.peek_inbox()
        if item is None:
            return None
        item.result = result
        item.done = True
        item.event.set()
        self._append_done(self.inbox.popleft())
        self.drain_kick.set()
        return item

    def inbox_choices_count(self) -> int:
        """Number of pending choice items in the inbox."""
        return sum(1 for item in self.inbox if item.kind == "choices" and not item.done)

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

        # Session age: use earliest history entry if available, else last_activity
        if self.history:
            first_action = min(h.timestamp for h in self.history)
            session_age = now - first_action
        elif self.last_tool_call > 0:
            session_age = now - self.last_tool_call + (self.tool_call_count * 2)  # rough estimate
        else:
            session_age = now - self.last_activity

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

    def in_use_voices(self) -> set[str]:
        """Return the set of voice_override values currently assigned to active sessions."""
        with self._lock:
            return {
                s.voice_override
                for s in self.sessions.values()
                if s.voice_override is not None
            }

    def in_use_emotions(self) -> set[str]:
        """Return the set of emotion_override values currently assigned to active sessions."""
        with self._lock:
            return {
                s.emotion_override
                for s in self.sessions.values()
                if s.emotion_override is not None
            }

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
                # Choice indicator (with inbox queue count badge)
                inbox_count = session.inbox_choices_count()
                if session.active:
                    if inbox_count > 1:
                        indicator = f" [bold {success}]o+{inbox_count - 1}[/bold {success}]"
                    else:
                        indicator = f" [bold {success}]o[/bold {success}]"
                elif inbox_count > 0:
                    # Has queued choices but isn't displaying yet
                    indicator = f" [bold {success}]+{inbox_count}[/bold {success}]"
                else:
                    indicator = ""
                # Health indicator (only when not showing active choices)
                health = getattr(session, 'health_status', 'healthy')
                if not session.active:
                    if health == "warning":
                        indicator = f" [bold {warning}]![/bold {warning}]"
                    elif health == "unresponsive":
                        indicator = f" [bold {error}]x[/bold {error}]"
                if sid == self.active_session_id:
                    parts.append(f"[bold {accent}]> {name}[/bold {accent}]{indicator}")
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
