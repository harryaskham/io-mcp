"""Chat bubble view for io-mcp TUI.

A vertical feed showing all agent interactions chronologically:
- Agent speech (speak/speak_async) as text bubbles
- Choice presentations with inline selection
- User messages with status indicators (queued/flushed)
- System events (tool calls, settings changes)

Toggled with 'g' key. Shows #chat-feed above the normal #preamble/#choices.
When active, #main-content is hidden but choices still render normally.
"""

from __future__ import annotations

import time as _time
from typing import TYPE_CHECKING

from textual.app import ComposeResult
from textual.widgets import Label, ListItem, ListView

from .themes import DEFAULT_SCHEME, get_scheme
from .widgets import _safe_action

if TYPE_CHECKING:
    from .app import IoMcpApp
    from ..session import Session


# ─── Chat Bubble Item ────────────────────────────────────────────────

class ChatBubbleItem(ListItem):
    """A single item in the chat feed.

    Kinds:
        speech   - Agent spoke text (speak/speak_async)
        choices  - Agent presented choices (may be resolved or pending)
        user_msg - User queued a message
        system   - System event (tool call, status update)
    """

    def __init__(self, kind: str, text: str, timestamp: float,
                 detail: str = "", resolved: bool = False,
                 result: str = "", choices: list[dict] | None = None,
                 flushed: bool = False, agent_name: str = "agent",
                 **kwargs) -> None:
        super().__init__(**kwargs)
        self.bubble_kind = kind
        self.bubble_text = text
        self.bubble_timestamp = timestamp
        self.bubble_detail = detail
        self.bubble_resolved = resolved
        self.bubble_result = result
        self.bubble_choices = choices or []
        self.bubble_flushed = flushed
        self.agent_name = agent_name
        # Plain text for TTS readout (no markup, no timestamps)
        self.tts_text = self._make_tts_text()

    def _make_tts_text(self) -> str:
        """Build clean plain text for TTS readout."""
        if self.bubble_kind == "speech":
            return self.bubble_text
        elif self.bubble_kind == "choices":
            if self.bubble_resolved and self.bubble_result:
                return f"selected {self.bubble_result}"
            labels = ", ".join(c.get("label", "") for c in self.bubble_choices[:5])
            return f"{self.bubble_text}. {labels}"
        elif self.bubble_kind == "user_msg":
            status = "sent" if self.bubble_flushed else "queued"
            return f"you, {status}: {self.bubble_text}"
        elif self.bubble_kind == "system":
            return self.bubble_text
        return self.bubble_text

    def compose(self) -> ComposeResult:
        # Get color scheme from the app if available, otherwise use default
        try:
            s = get_scheme(self.app._color_scheme)
        except Exception:
            s = get_scheme(DEFAULT_SCHEME)

        if self.bubble_kind == "speech":
            # Agent speech bubble
            ts = _time.strftime("%H:%M", _time.localtime(self.bubble_timestamp))
            yield Label(
                f"[{s['fg_dim']}]{ts}[/{s['fg_dim']}]  "
                f"[{s['accent']}]{self.agent_name}[/{s['accent']}]  "
                f"{self.bubble_text}",
                classes="chat-bubble-text",
            )

        elif self.bubble_kind == "choices":
            # Choice presentation
            ts = _time.strftime("%H:%M", _time.localtime(self.bubble_timestamp))
            # Preamble
            yield Label(
                f"[{s['fg_dim']}]{ts}[/{s['fg_dim']}]  "
                f"[{s['warning']}]choices[/{s['warning']}]  "
                f"{self.bubble_text}",
                classes="chat-bubble-text",
            )
            # Choice options
            for i, c in enumerate(self.bubble_choices):
                label = c.get("label", "")
                summary = c.get("summary", "")
                is_selected = (self.bubble_resolved and
                               self.bubble_result == label)
                if is_selected:
                    yield Label(
                        f"       [{s['success']}]>[/{s['success']}] "
                        f"[bold {s['success']}]{label}[/bold {s['success']}]"
                        f"  [{s['fg_dim']}]{summary}[/{s['fg_dim']}]",
                        classes="chat-bubble-choice-selected",
                    )
                elif self.bubble_resolved:
                    yield Label(
                        f"         [{s['fg_dim']}]{label}[/{s['fg_dim']}]"
                        f"  [{s['fg_dim']}]{summary}[/{s['fg_dim']}]",
                        classes="chat-bubble-choice-dim",
                    )
                else:
                    yield Label(
                        f"       [{s['accent']}]{i+1}.[/{s['accent']}] "
                        f"{label}"
                        f"  [{s['fg_dim']}]{summary}[/{s['fg_dim']}]",
                        classes="chat-bubble-choice",
                    )
            if self.bubble_resolved and self.bubble_result:
                pass  # selected option is highlighted above
            elif not self.bubble_resolved:
                yield Label(
                    f"       [{s['warning']}]awaiting selection...[/{s['warning']}]",
                    classes="chat-bubble-pending",
                )

        elif self.bubble_kind == "user_msg":
            # User message with status indicator
            ts = _time.strftime("%H:%M", _time.localtime(self.bubble_timestamp))
            icon = f"[{s['success']}]\u2713[/{s['success']}]" if self.bubble_flushed else f"[{s['fg_dim']}]\u25cb[/{s['fg_dim']}]"
            yield Label(
                f"[{s['fg_dim']}]{ts}[/{s['fg_dim']}]  "
                f"{icon} "
                f"[{s['purple']}]you[/{s['purple']}]  "
                f"{self.bubble_text}",
                classes="chat-bubble-text",
            )

        elif self.bubble_kind == "system":
            # System event
            ts = _time.strftime("%H:%M", _time.localtime(self.bubble_timestamp))
            yield Label(
                f"[{s['fg_dim']}]{ts}  \u2022 {self.bubble_text}[/{s['fg_dim']}]",
                classes="chat-bubble-system",
            )


# ─── Chat View Mixin ─────────────────────────────────────────────────

class ChatViewMixin:
    """Mixin providing the chat bubble view for IoMcpApp.

    The chat feed (#chat-feed) is a top-level sibling of #main-content.
    When active, #main-content is hidden and #chat-feed is shown.
    The normal #preamble + #choices widgets are reused for selection —
    no duplicate choice widgets needed.
    """

    _chat_view_active: bool = False
    _chat_content_hash: str = ""  # Track content to avoid redundant rebuilds

    @_safe_action
    def action_chat_view(self: "IoMcpApp") -> None:
        """Toggle the chat bubble view."""
        chat_feed = self.query_one("#chat-feed")

        # Toggle off
        if self._chat_view_active:
            chat_feed.display = False
            self._chat_view_active = False
            if hasattr(self, '_chat_refresh_timer') and self._chat_refresh_timer:
                self._chat_refresh_timer.stop()
                self._chat_refresh_timer = None
            # Restore main-content height and inbox width from chat view overrides
            try:
                mc = self.query_one("#main-content")
                mc.styles.height = "1fr"
                mc.styles.max_height = None
                inbox = self.query_one("#inbox-list")
                inbox.styles.width = 30  # Restore default width
            except Exception:
                pass
            self._speak_ui("Chat view closed.")
            session = self._focused()
            if session and session.active:
                self._show_choices()
            else:
                self.query_one("#main-content").display = True
                self.query_one("#status").display = True
            return

        session = self._focused()
        if not session:
            self._speak_ui("No active session")
            return
        if getattr(session, 'input_mode', False) or getattr(session, 'voice_recording', False):
            return
        if self._in_settings or self._filter_mode:
            return

        self._tts.stop()

        # Determine if unified mode (all agents) or single-session mode
        all_sessions = list(self.manager.all_sessions()) if hasattr(self, 'manager') else []
        if len(all_sessions) > 1:
            self._chat_unified = True
            self._speak_ui("Chat view. Press g to close.")
        else:
            self._chat_unified = False
            self._speak_ui(f"Chat view for {session.name}. Press g to close.")

        # Show chat feed, hide all other views
        self.query_one("#main-content").display = False
        self.query_one("#inbox-list").display = False
        self.query_one("#preamble").display = False
        self.query_one("#pane-view").display = False
        self.query_one("#status").display = False
        self.query_one("#speech-log").display = False
        self.query_one("#agent-activity").display = False
        chat_feed.display = True
        self._chat_view_active = True

        # Build feed — unified or single-session
        if self._chat_unified:
            self._build_chat_feed(session, sessions=all_sessions)
        else:
            self._build_chat_feed(session)

        # If session has active choices, show them below the feed
        if session.active and session.choices:
            self._show_choices()  # This will show #main-content with auto height

        # Auto-refresh every 3 seconds
        self._chat_refresh_timer = self.set_interval(
            3.0, lambda: self._refresh_chat_feed())

    def _build_chat_feed(self: "IoMcpApp", session: "Session",
                         sessions: list["Session"] | None = None) -> None:
        """Build the chronological chat feed from session data."""
        try:
            feed = self.query_one("#chat-feed", ListView)
        except Exception:
            return

        # Update content hash so periodic refresh skips redundant rebuilds
        if sessions:
            parts = []
            for s in sessions:
                parts.append(self._chat_content_fingerprint(s))
            self._chat_content_hash = "||".join(parts)
        else:
            self._chat_content_hash = self._chat_content_fingerprint(session)

        feed.clear()
        items = self._collect_chat_items(session, sessions=sessions)

        for item in items:
            try:
                feed.append(item)
            except Exception:
                pass

        # Scroll to bottom
        try:
            if len(feed.children) > 0:
                feed.scroll_end(animate=False)
        except Exception:
            pass

    def _collect_chat_items(self: "IoMcpApp", session: "Session",
                            sessions: list["Session"] | None = None) -> list[ChatBubbleItem]:
        """Merge session data into a chronological list of ChatBubbleItems."""
        all_sessions = sessions if sessions else [session]
        raw_items: list[tuple[float, str, ChatBubbleItem]] = []

        for sess in all_sessions:
            name = sess.name or "agent"

            # 1. Speech log entries
            for entry in sess.speech_log:
                raw_items.append((
                    entry.timestamp,
                    "speech",
                    ChatBubbleItem(
                        kind="speech",
                        text=entry.text[:200],
                        timestamp=entry.timestamp,
                        agent_name=name,
                    ),
                ))

            # 2. Resolved inbox items (choices that were answered)
            for item in sess.inbox_done:
                if item.kind == "choices":
                    result_label = ""
                    if item.result:
                        result_label = item.result.get("selected", "")
                    raw_items.append((
                        item.timestamp,
                        "choices",
                        ChatBubbleItem(
                            kind="choices",
                            text=item.preamble[:200],
                            timestamp=item.timestamp,
                            resolved=True,
                            result=result_label,
                            choices=item.choices[:9],
                            agent_name=name,
                        ),
                    ))

            # 3. Pending choices — skip, they're shown via the normal #choices widget

            # 4. User messages (pending = queued, flushed = delivered)
            now = _time.time()
            for msg in sess.pending_messages:
                raw_items.append((
                    now,
                    "user_msg",
                    ChatBubbleItem(
                        kind="user_msg",
                        text=msg[:200],
                        timestamp=now,
                        flushed=False,
                        agent_name=name,
                    ),
                ))

            # 5. Activity log entries (tool calls, status updates)
            for entry in sess.activity_log:
                kind = entry.get("kind", "tool")
                if kind in ("speech", "selection"):
                    continue
                tool = entry.get("tool", "")
                detail = entry.get("detail", "")
                text = f"{tool}" + (f": {detail[:60]}" if detail else "")
                raw_items.append((
                    entry["timestamp"],
                    "system",
                    ChatBubbleItem(
                        kind="system",
                        text=text,
                        timestamp=entry["timestamp"],
                        agent_name=name,
                    ),
                ))

        # Sort by timestamp and return just the ChatBubbleItems
        raw_items.sort(key=lambda x: x[0])

        # Limit to last 200 items
        if len(raw_items) > 200:
            raw_items = raw_items[-200:]

        return [item for _, _, item in raw_items]

    def _chat_content_fingerprint(self: "IoMcpApp", session: "Session") -> str:
        """Compute a lightweight fingerprint of chat-relevant session data."""
        parts = [
            str(len(session.speech_log)),
            str(len(session.inbox_done)),
            str(len(session.inbox)),
            str(len(session.pending_messages)),
            str(len(session.activity_log)),
        ]
        if session.speech_log:
            parts.append(f"s{session.speech_log[-1].timestamp:.0f}")
        if session.inbox_done:
            parts.append(f"d{session.inbox_done[-1].timestamp:.0f}")
        if session.inbox:
            parts.append(f"i{session.inbox[-1].timestamp:.0f}")
        if session.activity_log:
            parts.append(f"a{session.activity_log[-1].get('timestamp', 0):.0f}")
        return "|".join(parts)

    def _refresh_chat_feed(self: "IoMcpApp") -> None:
        """Refresh the chat feed only if the session data has changed."""
        if not self._chat_view_active:
            return
        session = self._focused()
        if not session:
            return

        # Unified mode: collect from all sessions
        if getattr(self, '_chat_unified', False):
            all_sessions = list(self.manager.all_sessions()) if hasattr(self, 'manager') else [session]
            fingerprint = "||".join(self._chat_content_fingerprint(s) for s in all_sessions)
            if fingerprint == self._chat_content_hash:
                return
            self._chat_content_hash = fingerprint
            self._build_chat_feed(session, sessions=all_sessions)
        else:
            fingerprint = self._chat_content_fingerprint(session)
            if fingerprint == self._chat_content_hash:
                return
            self._chat_content_hash = fingerprint
            self._build_chat_feed(session)
