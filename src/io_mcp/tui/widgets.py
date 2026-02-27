"""Reusable widgets and constants for io-mcp TUI.

Contains the ChoiceItem list item, DwellBar progress indicator,
TextInputModal, EXTRA_OPTIONS constant, and the _safe_action decorator.
"""

from __future__ import annotations

import functools
from typing import Optional

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.events import MouseScrollDown, MouseScrollUp
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.widgets import Label, ListItem, ListView, Static, TextArea

from ..logging import get_logger, log_context, TUI_ERROR_LOG

_log = get_logger("io-mcp.tui.widgets", TUI_ERROR_LOG)


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
            err = f"{type(exc).__name__}: {str(exc)[:100]}"
            _log.error(
                "Error in %s: %s", fn.__name__, err,
                exc_info=True,
                extra={"context": log_context(tool_name=fn.__name__)},
            )
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
    {"label": "Interrupt agent", "summary": "Send text directly to the agent's tmux pane via tmux-cli"},
    {"label": "Multi select", "summary": "Toggle multiple choices then confirm -- do several things at once"},
    {"label": "Dismiss", "summary": "Mark this choice as done without responding (clears dead/stale items)"},
    {"label": "Branch to worktree", "summary": "Create a git worktree for isolated work on a new branch"},
    {"label": "Compact context", "summary": "Compact the agent's context window to free up space"},
    {"label": "Pane view", "summary": "Show live tmux pane output for the focused agent"},
    {"label": "History", "summary": "Review past selections for this session"},
    {"label": "Switch tab", "summary": "Scroll through agent tabs and select one"},
    {"label": "New agent", "summary": "Spawn a new Claude Code agent (local or remote)"},
    {"label": "View logs", "summary": "TUI errors, proxy logs, speech history"},
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

    def _format_label(self) -> str:
        """Build the formatted label string based on choice_index."""
        if self.choice_index > 0:
            num = str(self.choice_index)
            pad = " " * (3 - len(num))
            return f"{pad}[bold]{num}[/bold]  {self.choice_label}"
        elif self.choice_index == -(len(EXTRA_OPTIONS) - 1):
            return f"    [dim]›[/dim] {self.choice_label}"
        else:
            return f"    [dim]›[/dim] {self.choice_label}"

    def _format_summary(self) -> str:
        """Build the formatted summary string."""
        return f"       {self.choice_summary}" if self.choice_summary else ""

    def compose(self) -> ComposeResult:
        yield Label(self._format_label(), classes="choice-label")
        if self.choice_summary:
            yield Label(self._format_summary(), classes="choice-summary")

    def update_content(self, label: str, summary: str) -> None:
        """Update the label and summary text in-place without rebuilding the widget."""
        self.choice_label = label
        self.choice_summary = summary
        try:
            label_widget = self.query_one(".choice-label", Label)
            label_widget.update(self._format_label())
        except Exception:
            pass
        try:
            summary_widget = self.query_one(".choice-summary", Label)
            summary_widget.update(self._format_summary())
        except Exception:
            # No summary widget exists; if we now have summary text, we can't
            # add it without a rebuild — but this is fine for multi-select
            # where summaries don't change between toggles.
            pass


# ─── Inbox List Item Widget ───────────────────────────────────────────────────

class InboxListItem(ListItem):
    """A single item in the inbox list (left pane of two-column layout).

    Shows a status icon and truncated text. Icons:
    - Choice items: ● pending, ✓ done
    - Speech items: ♪ pending/playing, > done
    Active (currently displayed) item is highlighted. Done items are dimmed.
    In multi-agent mode, shows the agent/session name prefix.
    """

    def __init__(self, preamble: str, is_done: bool = False,
                 is_active: bool = False, inbox_index: int = 0,
                 n_choices: int = 0, session_name: str = "",
                 accent_color: str = "", kind: str = "choices",
                 session_id: str = "",
                 **kwargs) -> None:
        super().__init__(**kwargs)
        self.inbox_preamble = preamble
        self.is_done = is_done
        self.is_active = is_active
        self.inbox_index = inbox_index  # position in the inbox list
        self.n_choices = n_choices
        self.session_name = session_name  # agent name (shown in multi-agent mode)
        self.accent_color = accent_color  # color for agent name tag
        self.kind = kind  # "choices" or "speech"
        self.session_id = session_id  # session ID for message routing

    def compose(self) -> ComposeResult:
        # Status icon — varies by kind
        if self.kind == "speech":
            if self.is_active:
                icon = "[bold]♪[/bold]"
            elif self.is_done:
                icon = "[dim]>[/dim]"
            else:
                icon = "♪"
        else:  # choices
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


# ─── Text Input Modal ──────────────────────────────────────────────────────

# Sentinel value to indicate the user requested voice recording from within the modal
VOICE_REQUESTED = "__voice_requested__"


class TextInputModal(ModalScreen[Optional[str]]):
    """True popup modal for text input (freeform reply or agent message).

    Overlays on top of the main TUI, preventing background inbox/choice
    updates from disrupting the input. Dismisses with:
    - The entered text (on Enter)
    - None (on Escape / cancel)
    - VOICE_REQUESTED sentinel (on Space in message mode)

    Args:
        title: Prompt text shown above the text area.
        message_mode: If True, space triggers voice recording via the app.
        allow_voice: If True, show voice hint in the title bar.
        scheme: Color scheme dict for styled labels.
    """

    BINDINGS = [
        ("escape", "cancel", "Cancel"),
    ]

    DEFAULT_CSS = """
    TextInputModal {
        align: center middle;
    }

    TextInputModal > Vertical {
        width: 80%;
        max-width: 100;
        height: auto;
        max-height: 20;
        border: tall $accent;
        background: $surface;
        padding: 1 2;
    }

    TextInputModal > Vertical > #modal-title {
        width: 1fr;
        height: auto;
        margin-bottom: 1;
        text-style: bold;
    }

    TextInputModal > Vertical > #modal-hint {
        width: 1fr;
        height: auto;
        margin-bottom: 1;
    }

    TextInputModal > Vertical > SubmitTextArea {
        height: auto;
        min-height: 3;
        max-height: 10;
    }
    """

    def __init__(
        self,
        title: str = "Type your reply",
        message_mode: bool = False,
        allow_voice: bool = True,
        scheme: Optional[dict[str, str]] = None,
        on_text_changed: Optional[callable] = None,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._title_text = title
        self._message_mode = message_mode
        self._allow_voice = allow_voice
        self._scheme = scheme or {}
        self._on_text_changed = on_text_changed

    def compose(self) -> ComposeResult:
        s = self._scheme
        accent = s.get("accent", "#88c0d0")

        with Vertical():
            yield Label(
                f"[bold {accent}]{self._title_text}[/bold {accent}]",
                id="modal-title",
            )
            # Show hint for available actions
            hints = ["[dim]Enter[/dim] submit", "[dim]Esc[/dim] cancel"]
            if self._message_mode and self._allow_voice:
                hints.insert(1, "[dim]Space[/dim] voice")
            yield Label("  ".join(hints), id="modal-hint")
            yield SubmitTextArea(
                id="modal-text-input",
                soft_wrap=True,
                show_line_numbers=False,
                tab_behavior="focus",
            )

    def on_mount(self) -> None:
        """Focus the text area on mount."""
        self.query_one("#modal-text-input", SubmitTextArea).focus()

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        """Forward text changes to the callback for TTS readback."""
        if self._on_text_changed:
            try:
                self._on_text_changed(event.text_area.text)
            except Exception:
                pass

    def on_submit_text_area_submitted(
        self, event: SubmitTextArea.Submitted
    ) -> None:
        """Submit text on Enter."""
        text = event.text_area.text.strip()
        if text:
            self.dismiss(text)
        # If empty, do nothing — user can press Escape to cancel

    def action_cancel(self) -> None:
        """Cancel input on Escape."""
        self.dismiss(None)

    def on_key(self, event) -> None:
        """Intercept space in message mode to trigger voice recording."""
        if self._message_mode and event.key == "space":
            event.prevent_default()
            event.stop()
            # Dismiss with sentinel — the app will start voice recording
            self.dismiss(VOICE_REQUESTED)
