"""Chat bubble view for io-mcp TUI.

A vertical feed showing all agent interactions chronologically:
- Agent speech (speak/speak_async) as text bubbles
- Choice presentations with inline selection
- User messages with status indicators (queued/flushed)
- System events (tool calls, settings changes)

Toggled with 'g' key. Replaces the inbox/choices pane when active.
"""

from __future__ import annotations

import time as _time
from typing import TYPE_CHECKING

from textual import work
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Label, ListItem, ListView, Static, Input

from .themes import DEFAULT_SCHEME, get_scheme
from .widgets import ManagedListView, _safe_action

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
                 flushed: bool = False, **kwargs) -> None:
        super().__init__(**kwargs)
        self.bubble_kind = kind
        self.bubble_text = text
        self.bubble_timestamp = timestamp
        self.bubble_detail = detail
        self.bubble_resolved = resolved
        self.bubble_result = result
        self.bubble_choices = choices or []
        self.bubble_flushed = flushed
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
        s = get_scheme(DEFAULT_SCHEME)

        if self.bubble_kind == "speech":
            # Agent speech bubble
            ts = _time.strftime("%H:%M", _time.localtime(self.bubble_timestamp))
            yield Label(
                f"[{s['fg_dim']}]{ts}[/{s['fg_dim']}]  "
                f"[{s['accent']}]agent[/{s['accent']}]  "
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
    """Mixin providing the chat bubble view for IoMcpApp."""

    _chat_view_active: bool = False
    _chat_view_generation: int = 0

    @_safe_action
    def action_chat_view(self: "IoMcpApp") -> None:
        """Toggle the chat bubble view.

        Shows a chronological feed of all agent interactions for the
        focused session. Press g or Escape to close.
        """
        chat_view = self.query_one("#chat-view")

        # Toggle off
        if chat_view.display:
            chat_view.display = False
            self._chat_view_active = False
            if hasattr(self, '_chat_refresh_timer') and self._chat_refresh_timer:
                self._chat_refresh_timer.stop()
                self._chat_refresh_timer = None
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
        self._speak_ui(f"Chat view for {session.name}. Press g to close.")

        # Show chat view, hide main content and speech log
        self.query_one("#main-content").display = False
        self.query_one("#preamble").display = False
        self.query_one("#pane-view").display = False
        self.query_one("#status").display = False
        self.query_one("#speech-log").display = False
        self.query_one("#agent-activity").display = False
        chat_view.display = True
        self._chat_view_active = True

        # Build feed
        self._build_chat_feed(session)

        # Auto-refresh every 3 seconds
        self._chat_refresh_timer = self.set_interval(
            3.0, lambda: self._refresh_chat_feed())

    def _build_chat_feed(self: "IoMcpApp", session: "Session") -> None:
        """Build the chronological chat feed from session data."""
        try:
            feed = self.query_one("#chat-feed", ListView)
        except Exception:
            return

        feed.clear()
        items = self._collect_chat_items(session)

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

    def _collect_chat_items(self: "IoMcpApp", session: "Session") -> list[ChatBubbleItem]:
        """Merge all session data into a chronological list of ChatBubbleItems."""
        raw_items: list[tuple[float, str, ChatBubbleItem]] = []

        # 1. Speech log entries
        for entry in session.speech_log:
            raw_items.append((
                entry.timestamp,
                "speech",
                ChatBubbleItem(
                    kind="speech",
                    text=entry.text[:200],
                    timestamp=entry.timestamp,
                ),
            ))

        # 2. Resolved inbox items (choices that were answered)
        for item in session.inbox_done:
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
                        choices=item.choices[:9],  # cap at 9
                    ),
                ))

        # 3. Pending inbox items (choices waiting for selection)
        for item in session.inbox:
            if item.kind == "choices" and not item.done:
                raw_items.append((
                    item.timestamp,
                    "choices",
                    ChatBubbleItem(
                        kind="choices",
                        text=item.preamble[:200],
                        timestamp=item.timestamp,
                        resolved=False,
                        choices=item.choices[:9],
                    ),
                ))

        # 4. User messages (pending = queued, flushed = delivered)
        # pending_messages don't have timestamps, so use current time
        now = _time.time()
        for msg in session.pending_messages:
            raw_items.append((
                now,
                "user_msg",
                ChatBubbleItem(
                    kind="user_msg",
                    text=msg[:200],
                    timestamp=now,
                    flushed=False,
                ),
            ))

        # 5. Activity log entries (tool calls, status updates)
        for entry in session.activity_log:
            kind = entry.get("kind", "tool")
            if kind in ("speech", "selection"):
                continue  # already covered by speech_log and inbox_done
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
                ),
            ))

        # Sort by timestamp and return just the ChatBubbleItems
        raw_items.sort(key=lambda x: x[0])

        # Limit to last 200 items
        if len(raw_items) > 200:
            raw_items = raw_items[-200:]

        return [item for _, _, item in raw_items]

    def _refresh_chat_feed(self: "IoMcpApp") -> None:
        """Refresh the chat feed if the session data has changed."""
        if not self._chat_view_active:
            return
        session = self._focused()
        if not session:
            return
        # Simple refresh — rebuild the feed
        self._build_chat_feed(session)

    def _handle_chat_message_input(self: "IoMcpApp", message: str) -> None:
        """Handle a message submitted from the chat input box."""
        session = self._focused()
        if not session or not message.strip():
            return
        session.pending_messages.append(message.strip())
        self._speak_ui("Message queued")
        self._refresh_chat_feed()
