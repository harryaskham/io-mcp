"""Reusable widgets and constants for io-mcp TUI.

Contains the ChoiceItem list item, DwellBar progress indicator,
EXTRA_OPTIONS constant, and the _safe_action decorator.
"""

from __future__ import annotations

import functools

from textual.app import ComposeResult
from textual.events import MouseScrollDown, MouseScrollUp
from textual.reactive import reactive
from textual.widgets import Label, ListItem, ListView, Static, TextArea


# ─── Safe action decorator ────────────────────────────────────────────────

def _safe_action(fn):
    """Decorator that catches exceptions in TUI action methods.

    Logs the error and speaks it via TTS instead of crashing the app.
    """

    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        try:
            return fn(self, *args, **kwargs)
        except Exception as exc:
            import traceback
            err = f"{type(exc).__name__}: {str(exc)[:100]}"
            try:
                with open("/tmp/io-mcp-tui-error.log", "a") as f:
                    f.write(f"\n--- {fn.__name__} ---\n{traceback.format_exc()}\n")
            except Exception:
                pass
            try:
                self._tts.speak_async(f"Error in {fn.__name__}: {err}")
            except Exception:
                pass
    return wrapper


# ─── Managed ListView (app-controlled scroll routing) ─────────────────────

class ManagedListView(ListView):
    """ListView that delegates mouse scroll events to the app.

    The default ListView (via Widget._on_mouse_scroll_down/up) handles mouse
    scroll events locally and may call event.stop(), preventing the event from
    reaching the app-level on_mouse_scroll_down/up handlers.  This is a problem
    for the two-pane inbox/choices layout: when the inbox pane is focused and
    the user scrolls over the choices pane, the choices ListView eats the
    event instead of letting the app route it to the inbox list.

    By overriding the private handlers to be no-ops, all mouse scroll events
    bubble up to IoMcpApp.on_mouse_scroll_down/up which use _active_list_view()
    to dispatch to the correct pane.
    """

    def _on_mouse_scroll_down(self, event: MouseScrollDown) -> None:
        # Let the event bubble to the app — don't handle locally.
        pass

    def _on_mouse_scroll_up(self, event: MouseScrollUp) -> None:
        # Let the event bubble to the app — don't handle locally.
        pass


# ─── Extra options (negative indices) ──────────────────────────────────────

# Primary extras: always visible above the real choices
PRIMARY_EXTRAS = [
    {"label": "Record response", "summary": "Speak your reply (voice input)"},
]

# Secondary extras: hidden behind "More options ›" header
SECONDARY_EXTRAS = [
    {"label": "Queue message", "summary": "Type or speak a message to queue for the agent's next response"},
    {"label": "Multi select", "summary": "Toggle multiple choices then confirm -- do several things at once"},
    {"label": "Branch to worktree", "summary": "Create a git worktree for isolated work on a new branch"},
    {"label": "Compact context", "summary": "Compact the agent's context window to free up space"},
    {"label": "Pane view", "summary": "Show live tmux pane output for the focused agent"},
    {"label": "History", "summary": "Review past selections for this session"},
    {"label": "Switch tab", "summary": "Scroll through agent tabs and select one"},
    {"label": "New agent", "summary": "Spawn a new Claude Code agent (local or remote)"},
    {"label": "Dashboard", "summary": "Overview of all active agents"},
    {"label": "View logs", "summary": "TUI errors, proxy logs, speech history"},
    {"label": "Unified inbox", "summary": "All pending choices across all agents"},
    {"label": "Close tab", "summary": "Close the focused agent tab"},
    {"label": "Quick settings", "summary": "Speed, voice, notifications, restart"},
]

# "More options" toggle item
MORE_OPTIONS_ITEM = {"label": "More options ›", "summary": ""}

# Full list for backward compat (used by extras_count, all_items, etc.)
EXTRA_OPTIONS = SECONDARY_EXTRAS + PRIMARY_EXTRAS


# ─── Choice Item Widget ─────────────────────────────────────────────────────

class ChoiceItem(ListItem):
    """A single choice in the list."""

    def __init__(self, label: str, summary: str, index: int = 0,
                 display_index: int = 0, **kwargs) -> None:
        super().__init__(**kwargs)
        self.choice_label = label
        self.choice_summary = summary
        self.choice_index = index      # logical index (can be negative)
        self.display_index = display_index  # position in list widget

    def compose(self) -> ComposeResult:
        if self.choice_index > 0:
            # Real choice — right-aligned number
            num = str(self.choice_index)
            pad = " " * (3 - len(num))
            yield Label(f"{pad}[bold]{num}[/bold]  {self.choice_label}", classes="choice-label")
        elif self.choice_index == -(len(EXTRA_OPTIONS) - 1):
            # First extra option — add a dim separator above
            yield Label(f"    [dim]›[/dim] {self.choice_label}", classes="choice-label")
        else:
            # Extra option — dim arrow prefix
            yield Label(f"    [dim]›[/dim] {self.choice_label}", classes="choice-label")
        if self.choice_summary:
            yield Label(f"       {self.choice_summary}", classes="choice-summary")


# ─── Inbox List Item Widget ───────────────────────────────────────────────────

class InboxListItem(ListItem):
    """A single item in the inbox list (left pane of two-column layout).

    Shows a status icon (● pending, ✓ done) and truncated preamble text.
    Active (currently displayed) item is highlighted. Done items are dimmed.
    In multi-agent mode, shows the agent/session name prefix so the user
    knows which agent sent each item.
    """

    def __init__(self, preamble: str, is_done: bool = False,
                 is_active: bool = False, inbox_index: int = 0,
                 n_choices: int = 0, session_name: str = "",
                 accent_color: str = "", **kwargs) -> None:
        super().__init__(**kwargs)
        self.inbox_preamble = preamble
        self.is_done = is_done
        self.is_active = is_active
        self.inbox_index = inbox_index  # position in the inbox list
        self.n_choices = n_choices
        self.session_name = session_name  # agent name (shown in multi-agent mode)
        self.accent_color = accent_color  # color for agent name tag

    def compose(self) -> ComposeResult:
        # Status icon
        if self.is_active:
            icon = "[bold]●[/bold]"
        elif self.is_done:
            icon = "[dim]✓[/dim]"
        else:
            icon = "○"

        # Agent name prefix (only set in multi-agent mode)
        if self.session_name:
            accent = self.accent_color or "#88c0d0"
            if self.is_done:
                name_tag = f"[dim][{accent}]{self.session_name}[/{accent}][/dim] "
            else:
                name_tag = f"[{accent}]{self.session_name}[/{accent}] "
        else:
            name_tag = ""

        # Truncate preamble for the narrow left pane
        # Use shorter limit when agent name takes space
        max_len = 28 if self.session_name else 40
        text = self.inbox_preamble[:max_len] if self.inbox_preamble else "(no preamble)"
        if len(self.inbox_preamble) > max_len:
            text += "…"

        if self.is_done:
            yield Label(f" {icon} {name_tag}[dim]{text}[/dim]", classes="inbox-label")
        elif self.is_active:
            yield Label(f" {icon} {name_tag}{text}", classes="inbox-label")
        else:
            yield Label(f" {icon} {name_tag}{text}", classes="inbox-label")


# ─── Dwell Progress Bar ─────────────────────────────────────────────────────

class DwellBar(Static):
    """Shows countdown progress when dwell mode is active."""

    progress = reactive(0.0)
    dwell_time = reactive(0.0)

    def render(self) -> str:
        if self.dwell_time <= 0 or self.progress <= 0:
            return ""
        bar_width = 20
        filled = int(bar_width * self.progress)
        empty = bar_width - filled
        remaining = self.dwell_time * (1.0 - self.progress)
        bar = "━" * filled + "╌" * empty
        return f"  [{bar}] {remaining:.1f}s"


# ─── Submit-on-Enter TextArea ─────────────────────────────────────────────

class SubmitTextArea(TextArea):
    """TextArea that submits on Enter instead of inserting a newline.

    Posts a SubmitTextArea.Submitted message that the app handles.
    """

    class Submitted(TextArea.Changed):
        """Posted when the user presses Enter."""
        pass

    def _on_key(self, event) -> None:
        """Intercept Enter to submit instead of inserting a newline."""
        if event.key == "enter":
            event.prevent_default()
            event.stop()
            self.post_message(self.Submitted(text_area=self))
            return
        return super()._on_key(event)
