"""Reusable widgets and constants for io-mcp TUI.

Contains the ChoiceItem list item, DwellBar progress indicator,
EXTRA_OPTIONS constant, and the _safe_action decorator.
"""

from __future__ import annotations

import functools

from textual.app import ComposeResult
from textual.reactive import reactive
from textual.widgets import Label, ListItem, Static


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


# ─── Extra options (negative indices) ──────────────────────────────────────

EXTRA_OPTIONS = [
    {"label": "Queue message", "summary": "Type or speak a message to queue for the agent's next response"},
    {"label": "Multi select", "summary": "Toggle multiple choices then confirm — do several things at once"},
    {"label": "History", "summary": "Review past selections for this session"},
    {"label": "Notifications", "summary": "Check Android notifications"},
    {"label": "Switch tab", "summary": "Scroll through agent tabs and select one"},
    {"label": "Fast toggle", "summary": "Toggle speed between current and 1.8x"},
    {"label": "Voice toggle", "summary": "Quick-switch between voices"},
    {"label": "New agent", "summary": "Spawn a new Claude Code agent (local or remote)"},
    {"label": "Dashboard", "summary": "Overview of all active agents"},
    {"label": "Settings", "summary": "Open settings menu"},
    {"label": "Record response", "summary": "Speak your reply (voice input)"},
]


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
