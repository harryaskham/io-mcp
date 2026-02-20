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
from textual.widgets import Footer, Header, Input, Label, ListItem, ListView, Static

from .tts import TTSEngine


# ─── Choice Item Widget ─────────────────────────────────────────────────────


class ChoiceItem(ListItem):
    """A single choice in the list."""

    def __init__(self, label: str, summary: str, index: int = 0, **kwargs) -> None:
        super().__init__(**kwargs)
        self.choice_label = label  # raw label without number
        self.choice_summary = summary
        self.choice_index = index  # 0-based

    def compose(self) -> ComposeResult:
        yield Label(f"[bold]{self.choice_index + 1}. {self.choice_label}[/bold]", classes="choice-label")
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
        text-wrap: wrap;
    }

    #status {
        margin: 1 2;
        color: $warning;
        text-wrap: wrap;
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
        text-wrap: wrap;
    }

    ChoiceItem > .choice-summary {
        color: $text-muted;
        margin-left: 2;
        text-wrap: wrap;
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

    #freeform-input {
        margin: 1 2;
        display: none;
    }
    """

    BINDINGS = [
        Binding("j,down", "cursor_down", "Down", show=False),
        Binding("k,up", "cursor_up", "Up", show=False),
        Binding("enter", "select", "Select", show=True),
        Binding("i", "freeform_input", "Type reply", show=True),
        Binding("1", "pick_1", "", show=False),
        Binding("2", "pick_2", "", show=False),
        Binding("3", "pick_3", "", show=False),
        Binding("4", "pick_4", "", show=False),
        Binding("5", "pick_5", "", show=False),
        Binding("6", "pick_6", "", show=False),
        Binding("7", "pick_7", "", show=False),
        Binding("8", "pick_8", "", show=False),
        Binding("9", "pick_9", "", show=False),
        Binding("q,ctrl+c", "quit_app", "Quit", show=True),
    ]

    def __init__(
        self,
        tts: TTSEngine,
        freeform_tts: TTSEngine | None = None,
        freeform_delimiters: str = " .,;:!?",
        dwell_time: float = 0.0,
        scroll_debounce: float = 0.15,
        invert_scroll: bool = False,
        demo: bool = False,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._tts = tts
        self._freeform_tts = freeform_tts or tts
        self._freeform_delimiters = set(freeform_delimiters)
        self._scroll_debounce = scroll_debounce
        self._invert_scroll = invert_scroll
        self._demo = demo
        self._last_scroll_time: float = 0.0
        self._dwell_time = dwell_time

        # State for present_choices blocking
        self._choices: list[dict] = []
        self._preamble = ""
        self._selection: Optional[dict] = None
        self._selection_event = threading.Event()
        self._active = False

        # True while preamble/intro TTS is playing — suppress scroll TTS
        # so highlight changes during intro don't interrupt it
        self._intro_speaking = False

        # Freeform text input mode
        self._input_mode = False
        self._freeform_spoken_pos = 0  # how far we've spoken in the input

        # Dwell timer
        self._dwell_timer: Optional[Timer] = None
        self._dwell_start: float = 0.0

    def compose(self) -> ComposeResult:
        yield Header(name="io-mcp", show_clock=False)
        status_text = "Ready — demo mode" if self._demo else "Waiting for agent..."
        yield Label(status_text, id="status")
        yield Label("", id="preamble")
        yield ListView(id="choices")
        yield Input(placeholder="Type your reply, press Enter to send, Escape to cancel", id="freeform-input")
        yield DwellBar(id="dwell-bar")
        yield Static("↕ Scroll  ⏎ Select  i Type reply  j/k Navigate  q Quit", id="footer-help")

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
        self._intro_speaking = True

        # Build numbered labels for TTS
        numbered_labels = [
            f"{i+1}. {c.get('label', '')}" for i, c in enumerate(choices)
        ]
        # Full summary line per choice: "1. Label. Description."
        numbered_full = [
            f"{i+1}. {c.get('label', '')}. {c.get('summary', '')}"
            for i, c in enumerate(choices)
        ]

        # Build the "all options" readout: preamble + list of numbered titles
        titles_readout = " ".join(numbered_labels)
        full_intro = f"{preamble} Your options are: {titles_readout}"

        # Show UI immediately — don't wait for audio pregeneration
        self.call_from_thread(self._show_choices)

        # Pregenerate all audio clips in parallel
        all_texts = (
            [full_intro]
            + numbered_full  # for on-scroll readout (label + desc)
            + [f"Selected: {c.get('label', '')}" for c in choices]
        )
        self._tts.pregenerate(all_texts)

        # Speak preamble + all option titles
        self._tts.speak(full_intro)

        # Intro done — clear the flag so highlight TTS works again, then
        # read whichever item the user has currently highlighted (they may
        # have scrolled during the intro). Use speak_async so it's
        # interruptible if they scroll again immediately.
        self._intro_speaking = False
        list_view = self.query_one("#choices", ListView)
        idx = list_view.index or 0
        if idx < len(numbered_full):
            self._tts.speak_async(numbered_full[idx])

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
        for i, c in enumerate(self._choices):
            label = c.get("label", "???")
            summary = c.get("summary", "")
            list_view.append(ChoiceItem(label, summary, index=i))
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
        after_text = f"Selected: {label}" if self._demo else f"Selected: {label} — waiting for agent..."
        status.update(after_text)
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
        """Speak numbered label + description when highlight changes."""
        if not self._active or event.item is None:
            return
        # While the intro is still speaking (preamble + all titles + option 1),
        # don't fire TTS for highlight changes — it would interrupt the intro.
        # The visual highlight still moves normally.
        if self._intro_speaking:
            return
        if isinstance(event.item, ChoiceItem):
            # Read: "2. Commit everything. Stage and commit the fix."
            idx = event.item.choice_index
            text = f"{idx + 1}. {event.item.choice_label}. {event.item.choice_summary}"
            self._tts.speak_async(text)
            if self._dwell_time > 0:
                self._start_dwell()

    @on(ListView.Selected)
    def on_list_selected(self, event: ListView.Selected) -> None:
        """Handle Enter/click on a list item."""
        if not self._active:
            return
        self._do_select()

    def action_cursor_down(self) -> None:
        if self._input_mode:
            return
        list_view = self.query_one("#choices", ListView)
        if list_view.display:
            list_view.action_cursor_down()

    def action_cursor_up(self) -> None:
        if self._input_mode:
            return
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
        """Mouse scroll down → move cursor down (or up if inverted)."""
        if self._active and self._scroll_allowed():
            list_view = self.query_one("#choices", ListView)
            if list_view.display:
                if self._invert_scroll:
                    list_view.action_cursor_up()
                else:
                    list_view.action_cursor_down()
                event.prevent_default()
                event.stop()

    def on_mouse_scroll_up(self, event: MouseScrollUp) -> None:
        """Mouse scroll up → move cursor up (or down if inverted)."""
        if self._active and self._scroll_allowed():
            list_view = self.query_one("#choices", ListView)
            if list_view.display:
                if self._invert_scroll:
                    list_view.action_cursor_down()
                else:
                    list_view.action_cursor_up()
                event.prevent_default()
                event.stop()

    def action_select(self) -> None:
        if self._active and not self._input_mode:
            self._do_select()

    def action_freeform_input(self) -> None:
        """Switch to freeform text input mode."""
        if not self._active or self._input_mode:
            return
        self._input_mode = True
        self._freeform_spoken_pos = 0
        self._cancel_dwell()
        self._tts.stop()
        self._tts.speak_async("Type your reply")

        # Hide choices, show input
        self.query_one("#choices").display = False
        self.query_one("#dwell-bar").display = False
        inp = self.query_one("#freeform-input", Input)
        inp.value = ""
        inp.styles.display = "block"
        inp.focus()

    @on(Input.Changed, "#freeform-input")
    def on_freeform_changed(self, event: Input.Changed) -> None:
        """Read back new chunks when a delimiter is typed."""
        if not self._input_mode:
            return
        text = event.value
        if len(text) <= self._freeform_spoken_pos:
            # Deletion — reset spoken position to current length
            self._freeform_spoken_pos = len(text)
            return
        # Check if the last character typed is a delimiter
        if text and text[-1] in self._freeform_delimiters:
            # Speak the new chunk since last spoken position
            chunk = text[self._freeform_spoken_pos:].strip()
            if chunk:
                self._freeform_tts.stop()
                self._freeform_tts.speak_async(chunk)
            self._freeform_spoken_pos = len(text)

    @on(Input.Submitted, "#freeform-input")
    def on_freeform_submitted(self, event: Input.Submitted) -> None:
        """Handle Enter in freeform input."""
        text = event.value.strip()
        if not text:
            return
        self._input_mode = False
        event.input.styles.display = "none"

        # Stop any freeform readback, confirm with main TTS
        self._freeform_tts.stop()
        self._tts.stop()
        self._tts.speak_async(f"Selected: {text}")

        self._selection = {"selected": text, "summary": "(freeform input)"}
        self._selection_event.set()
        self._show_waiting(text)

    def _cancel_freeform(self) -> None:
        """Cancel freeform input and return to choices."""
        self._input_mode = False
        self._freeform_tts.stop()
        inp = self.query_one("#freeform-input", Input)
        inp.styles.display = "none"
        self.query_one("#choices").display = True
        list_view = self.query_one("#choices", ListView)
        list_view.focus()
        if self._dwell_time > 0:
            self.query_one("#dwell-bar").display = True
            self._start_dwell()
        self._tts.speak_async("Cancelled. Back to choices.")

    def on_key(self, event) -> None:
        """Handle Escape in freeform input mode."""
        if self._input_mode and event.key == "escape":
            self._cancel_freeform()
            event.prevent_default()
            event.stop()

    def _pick_by_number(self, n: int) -> None:
        """Immediately select option by 1-based number."""
        if not self._active or self._input_mode:
            return
        idx = n - 1
        if idx < 0 or idx >= len(self._choices):
            return
        # Move highlight to that item first
        list_view = self.query_one("#choices", ListView)
        list_view.index = idx
        self._do_select()

    def action_pick_1(self) -> None: self._pick_by_number(1)
    def action_pick_2(self) -> None: self._pick_by_number(2)
    def action_pick_3(self) -> None: self._pick_by_number(3)
    def action_pick_4(self) -> None: self._pick_by_number(4)
    def action_pick_5(self) -> None: self._pick_by_number(5)
    def action_pick_6(self) -> None: self._pick_by_number(6)
    def action_pick_7(self) -> None: self._pick_by_number(7)
    def action_pick_8(self) -> None: self._pick_by_number(8)
    def action_pick_9(self) -> None: self._pick_by_number(9)

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
        self._tts.speak_async(f"Selected: {label}")

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
