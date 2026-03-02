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

from ..logging import get_logger, TUI_ERROR_LOG
from .themes import DEFAULT_SCHEME, get_scheme
from .widgets import _safe_action

if TYPE_CHECKING:
    from .app import IoMcpApp
    from ..session import Session

_log = get_logger("io-mcp.tui.chat", TUI_ERROR_LOG)


# ─── Chat Bubble Item ────────────────────────────────────────────────

class ChatBubbleItem(ListItem):
    """A single styled item in the chat feed ListView.

    Each bubble represents one chronological event in an agent session.
    Rendering uses the app's active color scheme for accent colors, borders,
    and dim text. A ``tts_text`` attribute is computed at init time for
    scroll-readout TTS so the TTS engine can pre-generate audio.

    Kinds:
        header   - Session header showing name, cwd, connection time.
        speech   - Agent spoke text (speak/speak_async).
        choices  - Agent presented choices (may be resolved or pending).
        user_msg - User queued a message (pending or flushed).
        system   - System event (tool call, ambient update, status).

    Attributes:
        bubble_kind: The event kind string (``"header"``, ``"speech"``, etc.).
        bubble_text: Primary display text (full length, no truncation).
        bubble_timestamp: Unix timestamp of the event.
        bubble_detail: Optional secondary info (e.g. cwd for headers).
        bubble_resolved: Whether a choices item has been answered.
        bubble_result: The selected choice label, if resolved.
        bubble_choices: List of choice dicts (``{"label": ..., "summary": ...}``).
        bubble_flushed: Whether a user_msg was delivered to the agent.
        agent_name: Display name of the agent session.
        tts_text: Pre-computed plain text for TTS readout (no markup).
    """

    def __init__(self, kind: str, text: str, timestamp: float,
                 detail: str = "", resolved: bool = False,
                 result: str = "", choices: list[dict] | None = None,
                 flushed: bool = False, agent_name: str = "agent",
                 freeform: bool = False,
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
        self.bubble_freeform = freeform
        # Plain text for TTS readout (no markup, no timestamps)
        self.tts_text = self._make_tts_text()

    def _make_tts_text(self) -> str:
        """Build clean plain text for TTS readout."""
        if self.bubble_kind == "header":
            return f"{self.agent_name} session"
        elif self.bubble_kind == "speech":
            return self.bubble_text
        elif self.bubble_kind == "choices":
            if self.bubble_freeform and self.bubble_resolved and self.bubble_result:
                return f"replied: {self.bubble_result}"
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
        """Render the bubble's Textual widgets based on its kind.

        Yields styled Label widgets appropriate to the bubble kind:
        - ``header``: Dim divider line with agent name, cwd, connection age.
        - ``speech``: Agent name + timestamp header, then the speech text.
        - ``choices``: Preamble text followed by numbered choice labels.
          Resolved choices highlight the selected option; pending ones
          show an "awaiting selection" indicator.
        - ``user_msg``: Status icon (check/circle) + "you" label + text.
        - ``system``: Single dim inline line with timestamp and event text.

        The color scheme is read from ``self.app._color_scheme`` at render
        time, falling back to ``DEFAULT_SCHEME`` if unavailable. A CSS class
        ``-{kind}`` is added to the item for left-border color coding.

        Yields:
            Label widgets composing the visual bubble.
        """
        # Get color scheme from the app if available, otherwise use default
        try:
            s = get_scheme(self.app._color_scheme)
        except Exception:
            s = get_scheme(DEFAULT_SCHEME)

        # Add kind-specific CSS class for left-border color coding
        kind_class = self.bubble_kind.replace("_", "-")
        self.add_class(f"-{kind_class}")

        ts = _time.strftime("%H:%M", _time.localtime(self.bubble_timestamp))

        if self.bubble_kind == "header":
            # Session header — dim, compact divider line
            parts = [self.agent_name]
            if self.bubble_detail:
                parts.append(self.bubble_detail)
            # Format relative connection time from timestamp
            if self.bubble_timestamp > 0:
                age = _time.time() - self.bubble_timestamp
                if age < 60:
                    age_str = f"connected {int(age)}s ago"
                elif age < 3600:
                    age_str = f"connected {int(age) // 60}m ago"
                else:
                    h = int(age) // 3600
                    m = (int(age) % 3600) // 60
                    age_str = f"connected {h}h{m:02d}m ago"
                parts.append(age_str)

            inner = " \u00b7 ".join(parts)
            yield Label(
                f"[{s['fg_dim']}]\u2500\u2500\u2500 {inner} \u2500\u2500\u2500[/{s['fg_dim']}]",
                classes="chat-bubble-header",
            )

        elif self.bubble_kind == "speech":
            # Agent speech bubble — accent left border
            yield Label(
                f"[{s['accent']}]{self.agent_name}[/{s['accent']}] "
                f"[{s['fg_dim']}]{ts}[/{s['fg_dim']}]",
                classes="chat-bubble-ts",
            )
            yield Label(self.bubble_text, classes="chat-bubble-text")

        elif self.bubble_kind == "choices":
            # Choice presentation — warning left border
            yield Label(
                f"[{s['warning']}]choices[/{s['warning']}] "
                f"[{s['fg_dim']}]{ts}[/{s['fg_dim']}]",
                classes="chat-bubble-ts",
            )
            if self.bubble_text:
                yield Label(self.bubble_text, classes="chat-bubble-text")
            # Choice options
            for i, c in enumerate(self.bubble_choices):
                label = c.get("label", "")
                summary = c.get("summary", "")
                is_selected = (self.bubble_resolved and
                               self.bubble_result == label)
                if is_selected:
                    yield Label(
                        f"  [{s['success']}]\u25b8[/{s['success']}] "
                        f"[bold {s['success']}]{label}[/bold {s['success']}]"
                        f"  [{s['fg_dim']}]{summary}[/{s['fg_dim']}]",
                        classes="chat-bubble-choice-selected",
                    )
                elif self.bubble_resolved:
                    yield Label(
                        f"    [{s['fg_dim']}]{label}[/{s['fg_dim']}]",
                        classes="chat-bubble-choice-dim",
                    )
                else:
                    yield Label(
                        f"  [{s['accent']}]{i+1}.[/{s['accent']}] "
                        f"{label}"
                        f"  [{s['fg_dim']}]{summary}[/{s['fg_dim']}]",
                        classes="chat-bubble-choice",
                    )
            if self.bubble_freeform and self.bubble_resolved and self.bubble_result:
                # Freeform reply — show connected reply line below the choices
                yield Label(
                    f"  [{s['border']}]╰─►[/{s['border']}] "
                    f"[bold {s['purple']}]{self.bubble_result}[/bold {s['purple']}]",
                    classes="chat-bubble-choice-selected",
                )
            if not self.bubble_resolved and not self.bubble_result:
                yield Label(
                    f"  [{s['warning']}]awaiting selection\u2026[/{s['warning']}]",
                    classes="chat-bubble-pending",
                )

        elif self.bubble_kind == "user_msg":
            # User message — purple left border, indented right
            icon = f"[{s['success']}]\u2713[/{s['success']}]" if self.bubble_flushed else f"[{s['fg_dim']}]\u25cb[/{s['fg_dim']}]"
            yield Label(
                f"{icon} [{s['purple']}]you[/{s['purple']}] "
                f"[{s['fg_dim']}]{ts}[/{s['fg_dim']}]",
                classes="chat-bubble-ts",
            )
            yield Label(self.bubble_text, classes="chat-bubble-text")

        elif self.bubble_kind == "system":
            # System event — no border, minimal inline text
            yield Label(
                f"[{s['fg_dim']}]{ts}  \u00b7 {self.bubble_text}[/{s['fg_dim']}]",
                classes="chat-bubble-system",
            )


# ─── Chat View Mixin ─────────────────────────────────────────────────

class ChatViewMixin:
    """Mixin providing the chat bubble view for IoMcpApp.

    Adds a chronological, scrollable feed of all agent interactions
    (speech, choices, user messages, system events) rendered as styled
    chat bubbles in a Textual ``ListView``.

    The chat feed (``#chat-feed``) is a top-level sibling of
    ``#main-content``. When active, ``#main-content`` is hidden and
    ``#chat-feed`` is shown. The normal ``#preamble`` + ``#choices``
    widgets are reused for active choice selection — no duplicate
    choice widgets are needed.

    Supports two display modes:
    - **Single-session**: shows one agent's history (default).
    - **Unified**: merges all agents' histories chronologically
      (auto-selected when multiple sessions exist).

    Uses incremental appending when possible to avoid expensive full
    rebuilds on every refresh. A 3-second auto-refresh timer polls
    for data changes; callers can also trigger immediate refreshes
    via ``_notify_chat_feed_update()``.

    Class Attributes:
        _chat_view_active: Whether the chat view is currently displayed.
        _chat_unified: True for multi-agent unified feed mode.
        _chat_content_hash: Fingerprint string to detect data changes.
        _chat_auto_scroll: Whether to auto-scroll to bottom on updates.
        _chat_last_item_count: Previous item count for incremental logic.
        _chat_base_fingerprint: Fingerprint of stable items for delta detection.
        _chat_force_full_rebuild: Flag to skip incremental append once.
        _chat_has_new_content: True when new content arrived while scrolled up.
    """

    _chat_view_active: bool = False
    _chat_unified: bool = False  # True = show all agents' data in unified feed
    _chat_content_hash: str = ""  # Track content to avoid redundant rebuilds
    _chat_auto_scroll: bool = True  # Auto-scroll to bottom on new content
    _chat_last_item_count: int = 0  # Track item count for incremental appends
    _chat_base_fingerprint: str = ""  # Fingerprint of "stable" data (detect modifications)
    _chat_force_full_rebuild: bool = False  # Set by external code to skip incremental
    _chat_has_new_content: bool = False  # New content below scroll position

    @_safe_action
    def action_chat_view(self: "IoMcpApp") -> None:
        """Toggle the chat bubble view on or off.

        When toggling **on**:
        - Hides ``#main-content``, ``#inbox-list``, ``#preamble``, and other
          views; shows ``#chat-feed`` and ``#chat-input-bar``.
        - Determines display mode: unified (all agents) if multiple sessions
          exist, otherwise single-session for the focused agent.
        - Builds the initial feed via ``_build_chat_feed()``.
        - If the focused session has active choices, shows them below the feed.
        - Starts a 3-second auto-refresh timer.

        When toggling **off**:
        - Hides ``#chat-feed`` and ``#chat-input-bar``; restores normal views.
        - Stops the auto-refresh timer.
        - Restores ``#main-content`` layout (height, inbox width).
        - Re-shows active choices if any, otherwise restores default view.

        Guards against activation when the session is in input mode,
        voice recording, settings menu, or filter mode.

        Side effects:
            Stops any current TTS playback on activation.
            Speaks a UI notification on toggle.
        """
        chat_feed = self.query_one("#chat-feed")

        # Toggle off
        if self._chat_view_active:
            _log.info("action_chat_view: toggling OFF")
            chat_feed.display = False
            self._chat_view_active = False
            # Hide chat-specific widgets
            try:
                self.query_one("#chat-choices").display = False
                self.query_one("#chat-input-bar").display = False
            except Exception:
                pass
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

        _log.info("action_chat_view: toggling ON")

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
        # Show chat input bar
        try:
            self.query_one("#chat-input-bar").display = True
        except Exception:
            pass
        self._chat_view_active = True
        self._chat_auto_scroll = True  # Start at bottom when opening chat view
        self._chat_has_new_content = False  # Clear new-content indicator
        self._chat_last_item_count = 0  # Force full build on open
        self._chat_base_fingerprint = ""  # Reset incremental tracker
        self._update_chat_new_indicator()  # Hide indicator label

        # Build feed — unified or single-session
        if self._chat_unified:
            self._build_chat_feed(session, sessions=all_sessions)
        else:
            self._build_chat_feed(session)

        # If session has active choices, show them below the feed
        if session.active and session.choices:
            self._show_choices()  # This will show #main-content with auto height

        # Auto-refresh every 3 seconds (stop existing timer first)
        if hasattr(self, '_chat_refresh_timer') and self._chat_refresh_timer:
            self._chat_refresh_timer.stop()
        self._chat_refresh_timer = self.set_interval(
            3.0, lambda: self._refresh_chat_feed())

    def _chat_feed_is_at_bottom(self: "IoMcpApp") -> bool:
        """Check if the chat feed ListView is scrolled to the bottom.

        Used by ``_build_chat_feed()`` to decide whether to auto-scroll
        after adding new items. If the user has scrolled up to read
        history, auto-scroll is suppressed to avoid yanking them away
        from what they're reading.

        Uses a threshold of 5 lines — the feed is considered "at bottom"
        if the scroll position is within 5 lines of the maximum, or if
        there's not enough content to scroll at all.

        Returns:
            True if the user is at or near the bottom (auto-scroll
            should happen), False if they've scrolled up. Defaults to
            True if the feed can't be queried (e.g. not mounted yet).
        """
        try:
            feed = self.query_one("#chat-feed", ListView)
            # ListView inherits from ScrollView — check scroll position
            # max_scroll_y is the maximum scroll offset; scroll_y is current
            max_y = feed.max_scroll_y
            cur_y = feed.scroll_y
            # Consider "at bottom" if within 5 lines of the end, or if
            # there's not enough content to scroll at all
            threshold = 5
            return max_y <= 0 or cur_y >= max_y - threshold
        except Exception:
            return True  # Default to auto-scroll if we can't determine

    def _build_chat_feed(self: "IoMcpApp", session: "Session",
                         sessions: list["Session"] | None = None) -> None:
        """Build or incrementally update the chronological chat feed.

        Collects all chat-relevant data from the session(s) into
        ``ChatBubbleItem`` widgets and populates the ``#chat-feed``
        ``ListView``.

        Uses an optimized incremental append strategy when possible:
        items are only appended (not rebuilt) if the base fingerprint
        is unchanged, item count didn't decrease, the 200-item cap
        wasn't hit, and we're not in unified mode. Falls back to a
        full clear-and-rebuild otherwise.

        After populating, pre-generates TTS audio for recent items
        (last 20 on full rebuild, delta items on incremental) so
        scroll readout is instant from cache.

        Args:
            session: The primary/focused session to build the feed for.
            sessions: If provided, builds a unified feed merging all
                sessions chronologically. Pass ``None`` for single-session
                mode.

        Side effects:
            - Clears and repopulates ``#chat-feed`` (full rebuild) or
              appends new items (incremental).
            - Updates ``_chat_last_item_count`` and
              ``_chat_base_fingerprint`` tracking state.
            - Scrolls to bottom if the user was already at the bottom.
            - Triggers TTS pregeneration in a background worker.
            - Clears ``_chat_force_full_rebuild`` flag after checking it.
        """
        try:
            feed = self.query_one("#chat-feed", ListView)
        except Exception:
            return

        # Check scroll position BEFORE clearing so we know if user was at bottom
        was_at_bottom = self._chat_feed_is_at_bottom()

        items = self._collect_chat_items(session, sessions=sessions)

        # Determine whether we can do an incremental append
        # Conditions for incremental:
        #   1. Feed already has items (not first build)
        #   2. New item count >= old item count (items were added, not removed)
        #   3. Base fingerprint unchanged (existing items not modified)
        #   4. Not in unified mode (multi-session sorting makes incremental unreliable)
        #   5. Old count was below the 200-item cap (otherwise truncation shifts items)
        #   6. No explicit force-full-rebuild flag set
        can_append = False
        old_count = self._chat_last_item_count

        if (not self._chat_force_full_rebuild
                and old_count > 0
                and old_count < 200  # At cap, new items cause front truncation
                and len(items) >= old_count
                and sessions is None):
            # Compute base fingerprint for all relevant sessions
            base_fp = self._chat_base_fingerprint_for(session)
            if base_fp == self._chat_base_fingerprint:
                can_append = True

        # Clear the force flag after checking it
        self._chat_force_full_rebuild = False

        if can_append:
            # Incremental append — only add new items
            delta = items[old_count:]
            _log.info("_build_chat_feed: incremental append", extra={"context": {
                "old_count": old_count,
                "new_count": len(items),
                "delta": len(delta),
                "was_at_bottom": was_at_bottom,
            }})
            for item in delta:
                try:
                    feed.append(item)
                except Exception:
                    pass

            # Pregenerate TTS only for the new items
            try:
                tts_texts = set()
                for item in delta:
                    t = getattr(item, 'tts_text', '')
                    if t and len(t) < 200:
                        tts_texts.add(t)
                if tts_texts and hasattr(self, '_pregenerate_ui_worker'):
                    self._pregenerate_ui_worker(list(tts_texts))
            except Exception:
                pass
        else:
            # Full rebuild — clear and re-add everything
            feed.clear()

            _log.info("_build_chat_feed: full rebuild", extra={"context": {
                "n_items": len(items),
                "unified": sessions is not None,
                "was_at_bottom": was_at_bottom,
                "auto_scroll": self._chat_auto_scroll,
                "old_count": old_count,
            }})

            for item in items:
                try:
                    feed.append(item)
                except Exception:
                    pass

            # Pregenerate TTS for visible chat bubble items so scroll readout
            # is instant (cache hit) instead of silent (cache miss → API call).
            # Only pregenerate the last ~20 items since the user is most likely
            # to scroll through recent history.
            try:
                tts_texts = set()
                recent = items[-20:] if len(items) > 20 else items
                for item in recent:
                    t = getattr(item, 'tts_text', '')
                    if t and len(t) < 200:  # skip very long texts
                        tts_texts.add(t)
                if tts_texts and hasattr(self, '_pregenerate_ui_worker'):
                    self._pregenerate_ui_worker(list(tts_texts))
            except Exception:
                pass

        # Update trackers for next incremental check
        self._chat_last_item_count = len(items)
        if sessions is None:
            self._chat_base_fingerprint = self._chat_base_fingerprint_for(session)
        else:
            # Unified mode: store combined base fingerprint
            self._chat_base_fingerprint = "||".join(
                self._chat_base_fingerprint_for(s) for s in sessions
            )

        # Only scroll to bottom if user was already at the bottom
        # (respects their scroll position if they scrolled up to read history)
        had_new_items = len(items) > old_count
        try:
            if len(feed.children) > 0 and was_at_bottom:
                feed.scroll_end(animate=False)
                self._chat_auto_scroll = True
                # Clear the indicator since we're at the bottom
                if self._chat_has_new_content:
                    self._chat_has_new_content = False
                    self._update_chat_new_indicator()
            elif not was_at_bottom:
                self._chat_auto_scroll = False
                # Show "↓ New" indicator if new items arrived while scrolled up
                if had_new_items and not self._chat_has_new_content:
                    self._chat_has_new_content = True
                    self._update_chat_new_indicator()
        except Exception:
            pass

    def _collect_chat_items(self: "IoMcpApp", session: "Session",
                            sessions: list["Session"] | None = None) -> list[ChatBubbleItem]:
        """Merge session data into a chronological list of ChatBubbleItems.

        Iterates over one or more sessions and collects all displayable
        events into ``ChatBubbleItem`` instances, then sorts them by
        timestamp. The result is capped at the most recent 200 items.

        Collected event sources per session (in order):
        1. **Session header** — synthetic item at registration time.
        2. **Speech log** — agent speak/speak_async calls (truncated to 200 chars).
        3. **Resolved inbox items** — answered choice presentations.
        4. **User messages** — both flushed (delivered) and pending (queued).
           Flushed messages use their delivery timestamp; pending messages
           use the current time.
        5. **Activity log** — tool calls, ambient updates, status events.
           Skips entries already represented as speech/selection/choices.

        Args:
            session: The primary session (used when ``sessions`` is None).
            sessions: Optional list of sessions for unified mode. If None,
                only ``session`` is processed.

        Returns:
            Chronologically sorted list of ``ChatBubbleItem`` instances,
            capped at 200 items (most recent kept).
        """
        all_sessions = sessions if sessions else [session]
        raw_items: list[tuple[float, str, ChatBubbleItem]] = []

        for sess in all_sessions:
            name = sess.name or "agent"

            # 0. Session header — appears at the very top of each session's feed
            reg_ts = getattr(sess, "registered_at", 0.0) or 0.0
            # Use registered_at if available, otherwise fall back to session creation time
            header_ts = reg_ts if reg_ts > 0 else sess.last_activity
            cwd = getattr(sess, "cwd", "") or ""
            raw_items.append((
                header_ts - 0.001,  # slightly before first real event
                "header",
                ChatBubbleItem(
                    kind="header",
                    text="",
                    timestamp=header_ts,
                    detail=cwd,
                    agent_name=name,
                ),
            ))

            # 1. Speech log entries
            for entry in sess.speech_log:
                raw_items.append((
                    entry.timestamp,
                    "speech",
                    ChatBubbleItem(
                        kind="speech",
                        text=entry.text,
                        timestamp=entry.timestamp,
                        agent_name=name,
                    ),
                ))

            # 2. Resolved inbox items (choices that were answered)
            for item in sess.inbox_done:
                if item.kind == "choices":
                    result_label = ""
                    is_freeform = False
                    if item.result:
                        result_label = item.result.get("selected", "")
                        is_freeform = item.result.get("summary", "") == "(freeform input)"
                    raw_items.append((
                        item.timestamp,
                        "choices",
                        ChatBubbleItem(
                            kind="choices",
                            text=item.preamble,
                            timestamp=item.timestamp,
                            resolved=True,
                            result=result_label,
                            choices=item.choices[:9],
                            agent_name=name,
                            freeform=is_freeform,
                        ),
                    ))

            # 3. Pending choices — skip, they're shown in the #chat-choices scrollable list

            # 4. User messages (pending = queued, flushed = delivered)
            # Show flushed messages first (delivered to agent, ✓ icon)
            for fm in getattr(sess, 'flushed_messages', []):
                raw_items.append((
                    fm.flushed_at,
                    "user_msg",
                    ChatBubbleItem(
                        kind="user_msg",
                        text=fm.text,
                        timestamp=fm.flushed_at,
                        flushed=True,
                        agent_name=name,
                    ),
                ))
            # Show pending messages (still queued, ○ icon)
            now = _time.time()
            for msg in sess.pending_messages:
                raw_items.append((
                    now,
                    "user_msg",
                    ChatBubbleItem(
                        kind="user_msg",
                        text=msg,
                        timestamp=now,
                        flushed=False,
                        agent_name=name,
                    ),
                ))

            # 5. Activity log entries (tool calls, status updates)
            for entry in sess.activity_log:
                kind = entry.get("kind", "tool")
                if kind in ("speech", "selection", "choices"):
                    continue
                tool = entry.get("tool", "")
                detail = entry.get("detail", "")
                if kind == "ambient":
                    # Ambient updates: show the phrase with a ~ prefix
                    text = f"~ {detail}" if detail else "~ working"
                else:
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
        """Compute a lightweight fingerprint of chat-relevant session data.

        Builds a pipe-delimited string from the lengths of all data sources
        (speech_log, inbox_done, inbox, pending_messages, flushed_messages,
        activity_log) plus the timestamp of the last entry in each non-empty
        source. Used by ``_refresh_chat_feed()`` to detect whether session
        data has changed since the last rebuild.

        This is intentionally cheap — no hashing or deep inspection. The
        fingerprint changes when items are added, removed, or when the
        last item's timestamp changes (e.g. a choice gets resolved).

        Args:
            session: The session to fingerprint.

        Returns:
            A pipe-delimited string encoding data source lengths and
            last-entry timestamps (e.g. ``"5|3|1|0|2|8|s1709312400|d1709312300"``).
        """
        flushed = getattr(session, 'flushed_messages', [])
        parts = [
            str(len(session.speech_log)),
            str(len(session.inbox_done)),
            str(len(session.inbox)),
            str(len(session.pending_messages)),
            str(len(flushed)),
            str(len(session.activity_log)),
        ]
        if session.speech_log:
            parts.append(f"s{session.speech_log[-1].timestamp:.0f}")
        if session.inbox_done:
            parts.append(f"d{session.inbox_done[-1].timestamp:.0f}")
        if session.inbox:
            parts.append(f"i{session.inbox[-1].timestamp:.0f}")
        if flushed:
            parts.append(f"f{flushed[-1].flushed_at:.0f}")
        if session.activity_log:
            parts.append(f"a{session.activity_log[-1].get('timestamp', 0):.0f}")
        return "|".join(parts)

    def _chat_base_fingerprint_for(self: "IoMcpApp", session: "Session") -> str:
        """Compute a fingerprint of the 'base' (existing) items.

        This captures the first item timestamps from each data source.
        If the base fingerprint hasn't changed, it means existing items
        weren't modified — only new items were appended. This lets us
        do an incremental append instead of a full rebuild.

        Changes that invalidate the base:
        - activity_log trimmed from front (first timestamp changes)
        - inbox_done trimmed from front
        - speech_log first item removed (shouldn't happen normally)
        - pending_messages count decreased (message flushed)
        """
        flushed = getattr(session, 'flushed_messages', [])
        parts = []
        # First-item timestamps — detect trimming from front
        if session.speech_log:
            parts.append(f"s0:{session.speech_log[0].timestamp:.0f}")
        if session.inbox_done:
            parts.append(f"d0:{session.inbox_done[0].timestamp:.0f}")
        if flushed:
            parts.append(f"f0:{flushed[0].flushed_at:.0f}")
        if session.activity_log:
            parts.append(f"a0:{session.activity_log[0].get('timestamp', 0):.0f}")
        # Pending messages count — decreases when flushed
        parts.append(f"pm:{len(session.pending_messages)}")
        return "|".join(parts)

    def _refresh_chat_feed(self: "IoMcpApp") -> None:
        """Refresh the chat feed if session data has changed.

        Called every 3 seconds by the auto-refresh timer set up in
        ``action_chat_view()``. Computes a content fingerprint for
        the relevant session(s) and compares it to the cached hash.
        If unchanged, the refresh is skipped to avoid unnecessary
        DOM manipulation.

        In unified mode, fingerprints are computed for all sessions
        and joined. In single-session mode, only the focused session
        is checked.

        Side effects:
            Calls ``_build_chat_feed()`` if data has changed, which
            may clear/rebuild or incrementally append to the ListView.
            Updates ``_chat_content_hash`` with the new fingerprint.

        Thread safety:
            Runs on the Textual event loop (main thread). Not thread-safe
            — must not be called from background threads.
        """
        if not self._chat_view_active:
            return
        session = self._focused()
        if not session:
            return

        # Check if user scrolled back to bottom (clears indicator)
        self._check_chat_scroll_position()

        # Unified mode: collect from all sessions
        if getattr(self, '_chat_unified', False):
            all_sessions = list(self.manager.all_sessions()) if hasattr(self, 'manager') else [session]
            fingerprint = "||".join(self._chat_content_fingerprint(s) for s in all_sessions)
            if fingerprint == self._chat_content_hash:
                return
            _log.info("_refresh_chat_feed: rebuilding (unified)")
            self._chat_content_hash = fingerprint
            self._build_chat_feed(session, sessions=all_sessions)
        else:
            fingerprint = self._chat_content_fingerprint(session)
            if fingerprint == self._chat_content_hash:
                return
            _log.info("_refresh_chat_feed: rebuilding")
            self._chat_content_hash = fingerprint
            self._build_chat_feed(session)

    def _notify_chat_feed_update(self: "IoMcpApp", session: "Session") -> None:
        """Force an immediate chat feed refresh when new content arrives.

        Called from ``_activate_speech_item()`` and ``notify_inbox_update()``
        to push new bubbles into the feed without waiting for the 3-second
        auto-refresh timer. Clears the cached content hash to ensure the
        next ``_refresh_chat_feed()`` call detects a change.

        In unified mode, any session's update triggers a refresh. In
        single-session mode, only refreshes if the updated session
        matches the currently focused session.

        Args:
            session: The session that has new content. Used to decide
                whether a refresh is needed in single-session mode.

        Side effects:
            Resets ``_chat_content_hash`` to empty and calls
            ``_refresh_chat_feed()``, which may rebuild or append to
            the feed.

        Thread safety:
            Must be called from the Textual event loop (main thread).
            Callers from background threads should use
            ``self.call_from_thread()`` to marshal onto the main thread.
        """
        if not self._chat_view_active:
            return
        focused = self._focused()
        if focused is None:
            return
        # In unified mode, any session's update triggers refresh.
        # In single-session mode, only refresh if the updated session is focused.
        if not getattr(self, '_chat_unified', False) and focused.session_id != session.session_id:
            return
        # Force a rebuild by clearing the content hash
        # (incremental append will still be used if base fingerprint is unchanged)
        self._chat_content_hash = ""
        self._refresh_chat_feed()

    def _update_chat_new_indicator(self: "IoMcpApp") -> None:
        """Show or hide the '↓ New' indicator below the chat feed.

        When ``_chat_has_new_content`` is True and the chat view is active,
        displays a styled label at the bottom of the ``#footer-status``
        bar indicating that new content is available below the current
        scroll position. Clears the label when the flag is False.

        Uses the app's color scheme for accent coloring. The indicator
        uses the ``#footer-status`` Static widget that's always docked
        at the bottom of the screen.
        """
        try:
            footer = self.query_one("#footer-status")
            if self._chat_has_new_content and self._chat_view_active:
                try:
                    s = get_scheme(self._color_scheme)
                except Exception:
                    s = get_scheme(DEFAULT_SCHEME)
                footer.update(
                    f"[bold {s['accent']}]↓ New[/bold {s['accent']}]"
                    f"  [{s['fg_dim']}]press G to scroll down[/{s['fg_dim']}]"
                )
            else:
                footer.update("")
        except Exception:
            pass

    def action_chat_scroll_bottom(self: "IoMcpApp") -> None:
        """Scroll the chat feed to the bottom and clear the new-content indicator.

        Bound to the ``G`` key. Scrolls the ``#chat-feed`` ListView to
        the end, re-enables auto-scroll, and clears the '↓ New' indicator
        if it was showing.

        Only active when the chat view is displayed. No-op otherwise.
        """
        if not self._chat_view_active:
            return
        try:
            feed = self.query_one("#chat-feed", ListView)
            feed.scroll_end(animate=False)
            self._chat_auto_scroll = True
            if self._chat_has_new_content:
                self._chat_has_new_content = False
                self._update_chat_new_indicator()
        except Exception:
            pass

    def _check_chat_scroll_position(self: "IoMcpApp") -> None:
        """Check if the user has scrolled back to the bottom and clear the indicator.

        Called from scroll event handlers or timers to detect when the user
        manually scrolls to the bottom of the chat feed. If they're at the
        bottom and the new-content indicator is showing, clears it.
        """
        if not self._chat_view_active or not self._chat_has_new_content:
            return
        if self._chat_feed_is_at_bottom():
            self._chat_has_new_content = False
            self._chat_auto_scroll = True
            self._update_chat_new_indicator()
