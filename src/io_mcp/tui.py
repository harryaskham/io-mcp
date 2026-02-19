"""TUI for io-mcp using textual.

Presents multi-choice options with scroll/keyboard navigation and
optional dwell-to-select. Designed for smart ring + earphones usage.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Optional

from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.events import MouseScrollDown, MouseScrollUp
from textual.reactive import reactive
from textual.timer import Timer
from textual.widget import Widget
from textual.widgets import Footer, Header, Label, ListItem, ListView, Static

from .tts import TTSEngine


# ─── Choice Item Widget ─────────────────────────────────────────────────────


class ChoiceItem(ListItem):
    """A single choice in the list."""

    def __init__(self, label: str, summary: str, **kwargs) -> None:
        super().__init__(**kwargs)
        self.choice_label = label
        self.choice_summary = summary

    def compose(self) -> ComposeResult:
        yield Label(f"[bold]{self.choice_label}[/bold]", classes="choice-label")
        yield Label(self.choice_summary, classes="choice-summary")


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
        bar = "█" * filled + "░" * empty
        return f"  [{bar}] {remaining:.1f}s"


# ─── Main TUI App ───────────────────────────────────────────────────────────


class IoMcpApp(App):
    """Textual app for io-mcp choice presentation."""

    CSS = """
    Screen {
        background: $surface;
    }

    #preamble {
        margin: 1 2;
        color: $success;
    }

    #status {
        margin: 1 2;
        color: $warning;
    }

    #choices {
        margin: 0 1;
        height: 1fr;
    }

    ChoiceItem {
        padding: 0 1;
        height: auto;
    }

    ChoiceItem > .choice-label {
        color: $text;
    }

    ChoiceItem > .choice-summary {
        color: $text-muted;
        margin-left: 2;
    }

    ChoiceItem.-highlight > .choice-label {
        color: $text;
        text-style: bold;
    }

    #dwell-bar {
        margin: 0 2;
        color: $warning;
        height: 1;
    }

    #footer-help {
        dock: bottom;
        height: 1;
        color: $text-muted;
        margin: 0 2;
    }
    """

    BINDINGS = [
        Binding("j,down", "cursor_down", "Down", show=False),
        Binding("k,up", "cursor_up", "Up", show=False),
        Binding("enter", "select", "Select", show=True),
        Binding("q,ctrl+c", "quit_app", "Quit", show=True),
    ]

    def __init__(
        self,
        tts: TTSEngine,
        dwell_time: float = 0.0,
        scroll_debounce: float = 0.15,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._tts = tts
        self._scroll_debounce = scroll_debounce
        self._last_scroll_time: float = 0.0
        self._dwell_time = dwell_time

        # State for present_choices blocking
        self._choices: list[dict] = []
        self._preamble = ""
        self._selection: Optional[dict] = None
        self._selection_event = threading.Event()
        self._active = False

        # Dwell timer
        self._dwell_timer: Optional[Timer] = None
        self._dwell_start: float = 0.0

    def compose(self) -> ComposeResult:
        yield Header(name="io-mcp", show_clock=False)
        yield Label("Waiting for Claude...", id="status")
        yield Label("", id="preamble")
        yield ListView(id="choices")
        yield DwellBar(id="dwell-bar")
        yield Static("↕ Scroll  ⏎ Select  j/k Navigate  q Quit", id="footer-help")

    def on_mount(self) -> None:
        self.title = "io-mcp"
        self.query_one("#preamble").display = False
        self.query_one("#choices").display = False
        self.query_one("#dwell-bar").display = False

    # ─── Choice presentation (called from MCP server thread) ─────────

    def present_choices(self, preamble: str, choices: list[dict]) -> dict:
        """Show choices and block until user selects. Thread-safe."""
        self._preamble = preamble
        self._choices = list(choices)
        self._selection = None
        self._selection_event.clear()
        self._active = True

        # Pregenerate all audio clips in parallel before showing UI
        labels = [c.get("label", "") for c in choices]
        all_texts = [preamble] + labels + [f"Selected: {l}" for l in labels]
        self._tts.pregenerate(all_texts)

        # Schedule UI update on the textual event loop
        self.call_from_thread(self._show_choices)

        # Speak preamble (now plays from cache instantly)
        self._tts.speak(preamble)

        # Block until selection
        self._selection_event.wait()
        self._active = False

        return self._selection or {"selected": "timeout", "summary": ""}

    def _show_choices(self) -> None:
        """Update the UI with new choices (runs on textual thread)."""
        # Update preamble
        preamble_widget = self.query_one("#preamble", Label)
        preamble_widget.update(self._preamble)
        preamble_widget.display = True

        # Hide status
        self.query_one("#status").display = False

        # Populate list
        list_view = self.query_one("#choices", ListView)
        list_view.clear()
        for c in self._choices:
            label = c.get("label", "???")
            summary = c.get("summary", "")
            list_view.append(ChoiceItem(label, summary))
        list_view.display = True
        list_view.index = 0
        list_view.focus()

        # Dwell
        if self._dwell_time > 0:
            dwell_bar = self.query_one("#dwell-bar", DwellBar)
            dwell_bar.dwell_time = self._dwell_time
            dwell_bar.progress = 0.0
            dwell_bar.display = True
            self._start_dwell()
        else:
            self.query_one("#dwell-bar").display = False

    def _show_waiting(self, label: str) -> None:
        """Show waiting state after selection."""
        self.query_one("#choices").display = False
        self.query_one("#preamble").display = False
        self.query_one("#dwell-bar").display = False
        status = self.query_one("#status", Label)
        status.update(f"Selected: {label} — waiting for Claude...")
        status.display = True

    def speak(self, text: str) -> None:
        """Non-blocking TTS (can be called from any thread)."""
        self._tts.speak(text)

    # ─── Dwell timer ─────────────────────────────────────────────────

    def _start_dwell(self) -> None:
        self._cancel_dwell()
        import time
        self._dwell_start = time.time()
        self._dwell_timer = self.set_interval(0.05, self._tick_dwell)

    def _cancel_dwell(self) -> None:
        if self._dwell_timer is not None:
            self._dwell_timer.stop()
            self._dwell_timer = None

    def _tick_dwell(self) -> None:
        import time
        if not self._active or self._dwell_time <= 0:
            self._cancel_dwell()
            return
        elapsed = time.time() - self._dwell_start
        progress = min(1.0, elapsed / self._dwell_time)
        dwell_bar = self.query_one("#dwell-bar", DwellBar)
        dwell_bar.progress = progress
        if progress >= 1.0:
            self._cancel_dwell()
            self._do_select()

    # ─── Event handlers ──────────────────────────────────────────────

    @on(ListView.Highlighted)
    def on_highlight_changed(self, event: ListView.Highlighted) -> None:
        """Speak label when highlight changes."""
        if not self._active or event.item is None:
            return
        if isinstance(event.item, ChoiceItem):
            self._tts.speak(event.item.choice_label)
            if self._dwell_time > 0:
                self._start_dwell()

    @on(ListView.Selected)
    def on_list_selected(self, event: ListView.Selected) -> None:
        """Handle Enter/click on a list item."""
        if not self._active:
            return
        self._do_select()

    def action_cursor_down(self) -> None:
        list_view = self.query_one("#choices", ListView)
        if list_view.display:
            list_view.action_cursor_down()

    def action_cursor_up(self) -> None:
        list_view = self.query_one("#choices", ListView)
        if list_view.display:
            list_view.action_cursor_up()

    def _scroll_allowed(self) -> bool:
        """Check if enough time has passed since the last scroll."""
        import time
        now = time.time()
        if now - self._last_scroll_time < self._scroll_debounce:
            return False
        self._last_scroll_time = now
        return True

    def on_mouse_scroll_down(self, event: MouseScrollDown) -> None:
        """Mouse scroll down → move cursor down."""
        if self._active and self._scroll_allowed():
            list_view = self.query_one("#choices", ListView)
            if list_view.display:
                list_view.action_cursor_down()
                event.prevent_default()
                event.stop()

    def on_mouse_scroll_up(self, event: MouseScrollUp) -> None:
        """Mouse scroll up → move cursor up."""
        if self._active and self._scroll_allowed():
            list_view = self.query_one("#choices", ListView)
            if list_view.display:
                list_view.action_cursor_up()
                event.prevent_default()
                event.stop()

    def action_select(self) -> None:
        if self._active:
            self._do_select()

    def action_quit_app(self) -> None:
        if self._active:
            self._cancel_dwell()
            self._selection = {"selected": "quit", "summary": "User quit"}
            self._selection_event.set()
        self.exit()

    def _do_select(self) -> None:
        """Finalize the current selection."""
        if not self._active or not self._choices:
            return
        self._cancel_dwell()
        list_view = self.query_one("#choices", ListView)
        idx = list_view.index or 0
        if idx >= len(self._choices):
            idx = 0
        chosen = self._choices[idx]
        label = chosen.get("label", "")
        summary = chosen.get("summary", "")

        self._tts.stop()
        self._tts.speak(f"Selected: {label}")

        self._selection = {"selected": label, "summary": summary}
        self._selection_event.set()

        self.call_from_thread(self._show_waiting, label) if not asyncio.get_event_loop().is_running() else self._show_waiting(label)


# ─── TUI Controller (public API for MCP server) ─────────────────────────────


class TUI:
    """Manages the textual app lifecycle.

    Used by the MCP server:
    - start() launches the textual app in a background thread
    - present_choices() blocks until user selects
    - speak() is non-blocking TTS
    - stop() shuts down
    """

    def __init__(self, local_tts: bool = False, dwell_time: float = 0.0):
        self._tts = TTSEngine(local=local_tts)
        self._app: Optional[IoMcpApp] = None
        self._thread: Optional[threading.Thread] = None
        self._dwell_time = dwell_time

    def start(self) -> None:
        """Start the textual app in a background thread."""
        self._app = IoMcpApp(
            tts=self._tts,
            dwell_time=self._dwell_time,
        )
        self._thread = threading.Thread(target=self._run_app, daemon=True)
        self._thread.start()

    def _run_app(self) -> None:
        assert self._app is not None
        self._app.run()

    def stop(self) -> None:
        """Stop the textual app."""
        if self._app is not None:
            try:
                self._app.exit()
            except Exception:
                pass
        self._tts.cleanup()

    def present_choices(self, preamble: str, choices: list[dict]) -> dict:
        """Show choices and block until user selects. Thread-safe."""
        if self._app is None:
            return {"selected": "error", "summary": "TUI not started"}
        return self._app.present_choices(preamble, choices)

    def speak(self, text: str) -> None:
        """Non-blocking TTS."""
        self._tts.speak(text)
