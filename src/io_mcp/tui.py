"""TUI for io-mcp using textual.

Presents multi-choice options with scroll/keyboard navigation and
optional dwell-to-select. Designed for smart ring + earphones usage.

Supports multiple concurrent agent sessions via tabs.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import threading
import time
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

from .session import Session, SessionManager, SpeechEntry
from .tts import PORTAUDIO_LIB, TTSEngine, _find_binary


# ─── Extra options (negative indices) ──────────────────────────────────────
EXTRA_OPTIONS = [
    {"label": "Record response", "summary": "Speak your reply (voice input)"},
    {"label": "Previous tab", "summary": "Switch to the previous session tab"},
    {"label": "Next tab", "summary": "Switch to the next session tab"},
    {"label": "Fast toggle", "summary": "Toggle speed between current and 1.8x"},
    {"label": "Voice toggle", "summary": "Quick-switch between voices"},
    {"label": "Settings", "summary": "Open settings menu"},
]
# Display order (top to bottom): -5=Record, -4=Prev tab, -3=Next tab, -2=Fast, -1=Voice, 0=Settings
# Logical index to array: ei = len(EXTRA_OPTIONS) - 1 + logical_index
# Reached by scrolling up past the first real option


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
        prefix = str(self.choice_index) if self.choice_index <= 0 else str(self.choice_index)
        yield Label(f"[bold]{prefix}. {self.choice_label}[/bold]", classes="choice-label")
        if self.choice_summary:
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


# ─── Settings state ─────────────────────────────────────────────────────────

class Settings:
    """Runtime settings managed via the in-TUI settings menu."""

    def __init__(self):
        self.speed = float(os.environ.get("TTS_SPEED", "1.0"))
        self.provider = os.environ.get("TTS_PROVIDER", "openai")
        if self.provider == "azure-speech":
            self.voice = os.environ.get("AZURE_SPEECH_VOICE", "en-US-Noa:MAI-Voice-1")
        else:
            self.voice = os.environ.get("OPENAI_TTS_VOICE", "sage")
        self._pre_fast_speed: float | None = None  # for fast toggle

    def apply_to_env(self):
        """Push current settings to env vars so TTS picks them up."""
        os.environ["TTS_SPEED"] = str(self.speed)
        os.environ["TTS_PROVIDER"] = self.provider
        if self.provider == "openai":
            os.environ["OPENAI_TTS_VOICE"] = self.voice
        else:
            os.environ["AZURE_SPEECH_VOICE"] = self.voice

    def get_voices(self) -> list[str]:
        if self.provider == "openai":
            return ["alloy", "ash", "ballad", "coral", "echo", "fable", "nova",
                    "onyx", "sage", "shimmer"]
        else:
            return ["en-US-Noa:MAI-Voice-1", "en-US-Teo:MAI-Voice-1"]

    def toggle_fast(self) -> str:
        if self._pre_fast_speed is not None:
            self.speed = self._pre_fast_speed
            self._pre_fast_speed = None
            msg = f"Speed reset to {self.speed}"
        else:
            self._pre_fast_speed = self.speed
            self.speed = 1.8
            msg = "Speed set to 1.8"
        self.apply_to_env()
        return msg

    def toggle_voice(self) -> str:
        voices = self.get_voices()
        if self.provider == "openai":
            # Toggle between sage, ballad, and original
            cycle = ["sage", "ballad"]
            if self.voice not in cycle:
                cycle.append(self.voice)
            idx = cycle.index(self.voice) if self.voice in cycle else -1
            self.voice = cycle[(idx + 1) % len(cycle)]
        else:
            idx = voices.index(self.voice) if self.voice in voices else 0
            self.voice = voices[(idx + 1) % len(voices)]
        self.apply_to_env()
        return f"Voice: {self.voice}"


# ─── Main TUI App ───────────────────────────────────────────────────────────

class IoMcpApp(App):
    """Textual app for io-mcp choice presentation with multi-session support."""

    CSS = """
    Screen {
        background: $surface;
    }

    #tab-bar {
        margin: 0 2;
        height: 1;
        color: $accent;
    }

    #preamble {
        margin: 1 2;
        color: $success;
        width: 1fr;
    }

    #status {
        margin: 1 2;
        color: $warning;
        width: 1fr;
    }

    #speech-log {
        margin: 0 2;
        height: auto;
        max-height: 6;
        color: $text-muted;
    }

    .speech-entry {
        color: $text-muted;
        margin: 0;
        padding: 0;
    }

    #choices {
        margin: 0 1;
        height: 1fr;
        overflow-x: hidden;
    }

    ChoiceItem {
        padding: 0 1;
        height: auto;
        width: 1fr;
    }

    ChoiceItem > .choice-label {
        color: $text;
        width: 1fr;
    }

    ChoiceItem > .choice-summary {
        color: $text-muted;
        margin-left: 2;
        width: 1fr;
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
        Binding("space", "voice_input", "Voice", show=True),
        Binding("s", "toggle_settings", "Settings", show=True),
        Binding("p", "replay_prompt", "Replay", show=False),
        Binding("P", "replay_prompt_full", "Replay all", show=False),
        Binding("l", "next_tab", "Next tab", show=False),
        Binding("h", "prev_tab", "Prev tab", show=False),
        Binding("n", "next_choices_tab", "Next choices", show=False),
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

        # Session manager
        self.manager = SessionManager()

        # Freeform text input
        self._freeform_spoken_pos = 0

        # Voice input
        self._voice_process: Optional[subprocess.Popen] = None
        self._voice_rec_file: Optional[str] = None

        # Settings (global, not per-session)
        self.settings = Settings()
        self._settings_items: list[dict] = []
        self._setting_edit_mode = False
        self._setting_edit_values: list[str] = []
        self._setting_edit_index: int = 0
        self._setting_edit_key: str = ""

        # Dwell timer
        self._dwell_timer: Optional[Timer] = None
        self._dwell_start: float = 0.0

        # Flag: is foreground currently speaking (blocks bg playback)
        self._fg_speaking = False

    # ─── Helpers to get focused session ────────────────────────────

    def _focused(self) -> Optional[Session]:
        """Get the currently focused session."""
        return self.manager.focused()

    def _is_focused(self, session_id: str) -> bool:
        """Check if a session is the focused one."""
        return self.manager.active_session_id == session_id

    # ─── Widget composition ────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield Header(name="io-mcp", show_clock=False)
        yield Static("", id="tab-bar")
        status_text = "Ready — demo mode" if self._demo else "Waiting for agent..."
        yield Label(status_text, id="status")
        yield Label("", id="preamble")
        yield Vertical(id="speech-log")
        yield ListView(id="choices")
        yield Input(placeholder="Type your reply, press Enter to send, Escape to cancel", id="freeform-input")
        yield DwellBar(id="dwell-bar")
        yield Static("↕ Scroll  ⏎ Select  i Type  ␣ Voice  s Settings  h/l Tabs  n Next  q Quit", id="footer-help")

    def on_mount(self) -> None:
        self.title = "io-mcp"
        self.query_one("#tab-bar").display = False
        self.query_one("#preamble").display = False
        self.query_one("#choices").display = False
        self.query_one("#dwell-bar").display = False
        self.query_one("#speech-log").display = False

    # ─── Tab bar rendering ─────────────────────────────────────────

    def _update_tab_bar(self) -> None:
        """Update the tab bar display."""
        tab_bar = self.query_one("#tab-bar", Static)
        if self.manager.count() <= 0:
            tab_bar.display = False
            return
        tab_bar.update(self.manager.tab_bar_text())
        # Only show tab bar when multiple sessions exist
        tab_bar.display = self.manager.count() > 1

    # ─── Speech log rendering ──────────────────────────────────────

    def _update_speech_log(self) -> None:
        """Update the speech log display for the focused session."""
        log_widget = self.query_one("#speech-log", Vertical)
        log_widget.remove_children()

        session = self._focused()
        if session is None:
            log_widget.display = False
            return

        # Show last 5 speech entries
        recent = session.speech_log[-5:]
        if not recent:
            log_widget.display = False
            return

        for entry in recent:
            label = Label(f"💬 {entry.text}", classes="speech-entry")
            log_widget.mount(label)
        log_widget.display = True

    # ─── Choice presentation (called from MCP server thread) ─────────

    def present_choices(self, session: Session, preamble: str, choices: list[dict]) -> dict:
        """Show choices and block until user selects. Thread-safe.

        Each session has its own selection_event so multiple sessions
        can block independently.
        """
        session.preamble = preamble
        session.choices = list(choices)
        session.selection = None
        session.selection_event.clear()
        session.active = True
        session.intro_speaking = True
        session.reading_options = False
        session.in_settings = False

        # Build the full list: extras + real choices
        session.extras_count = len(EXTRA_OPTIONS)
        session.all_items = list(EXTRA_OPTIONS) + session.choices

        # Build TTS texts
        numbered_labels = [
            f"{i+1}. {c.get('label', '')}" for i, c in enumerate(choices)
        ]
        numbered_full_all = []
        for i, c in enumerate(choices):
            s = c.get('summary', '')
            if s:
                numbered_full_all.append(f"{i+1}. {c.get('label', '')}. {s}")
            else:
                numbered_full_all.append(f"{i+1}. {c.get('label', '')}")

        titles_readout = " ".join(numbered_labels)
        full_intro = f"{preamble} Your options are: {titles_readout}"

        is_fg = self._is_focused(session.session_id)

        # Show UI immediately if this is the focused session
        if is_fg:
            self.call_from_thread(self._show_choices)

        # Update tab bar (session now has active choices indicator)
        self.call_from_thread(self._update_tab_bar)

        # Pregenerate per-option clips in background
        bg_texts = (
            numbered_full_all
            + [f"Selected: {c.get('label', '')}" for c in choices]
            + [f"{e['label']}. {e['summary']}" for e in EXTRA_OPTIONS if e.get('summary')]
        )
        pregen_thread = threading.Thread(
            target=self._tts.pregenerate, args=(bg_texts,), daemon=True
        )
        pregen_thread.start()

        if is_fg:
            # Foreground: speak intro and read options
            self._fg_speaking = True
            self._tts.speak(full_intro)

            session.intro_speaking = False
            session.reading_options = True
            for i, text in enumerate(numbered_full_all):
                if not session.reading_options or not session.active:
                    break
                self._tts.speak(text)
            session.reading_options = False
            self._fg_speaking = False

            # If user hasn't scrolled, read current highlight
            if session.active:
                self._speak_current_highlight(session)

            # Try playing any background queued speech
            self._try_play_background_queue()
        else:
            # Background: queue intro for later, read abbreviated version
            session.intro_speaking = False
            entry = SpeechEntry(text=full_intro)
            session.unplayed_speech.append(entry)
            session.speech_log.append(SpeechEntry(text=f"[choices] {preamble}"))

            # Try to speak in background if fg is idle
            self._try_play_background_queue()

        # Block until selection
        session.selection_event.wait()
        session.active = False
        self.call_from_thread(self._update_tab_bar)

        return session.selection or {"selected": "timeout", "summary": ""}

    def _speak_current_highlight(self, session: Session) -> None:
        """Read out the currently highlighted item."""
        if not self._is_focused(session.session_id):
            return
        try:
            list_view = self.query_one("#choices", ListView)
            idx = list_view.index or 0
            item = self._get_item_at_display_index(idx)
            if item:
                logical = item.choice_index
                if logical > 0:
                    ci = logical - 1
                    c = session.choices[ci]
                    s = c.get('summary', '')
                    text = f"{logical}. {c.get('label', '')}. {s}" if s else f"{logical}. {c.get('label', '')}"
                else:
                    ei = len(EXTRA_OPTIONS) - 1 + logical
                    if 0 <= ei < len(EXTRA_OPTIONS):
                        e = EXTRA_OPTIONS[ei]
                        text = f"{e['label']}. {e['summary']}"
                    else:
                        text = ""
                if text:
                    self._tts.speak_async(text)
        except Exception:
            pass

    def _get_item_at_display_index(self, idx: int) -> Optional[ChoiceItem]:
        """Get ChoiceItem at a display position."""
        list_view = self.query_one("#choices", ListView)
        if idx < 0 or idx >= len(list_view.children):
            return None
        item = list_view.children[idx]
        return item if isinstance(item, ChoiceItem) else None

    def _show_choices(self) -> None:
        """Update the UI with choices from the focused session (runs on textual thread)."""
        session = self._focused()
        if session is None:
            return

        preamble_widget = self.query_one("#preamble", Label)
        preamble_widget.update(session.preamble)
        preamble_widget.display = True

        self.query_one("#status").display = False

        list_view = self.query_one("#choices", ListView)
        list_view.clear()

        # Add extras (indices 0, -1, -2, -3)
        for i, e in enumerate(EXTRA_OPTIONS):
            logical_idx = -(len(EXTRA_OPTIONS) - 1 - i)  # -3, -2, -1, 0
            list_view.append(ChoiceItem(
                e["label"], e.get("summary", ""),
                index=logical_idx, display_index=i,
            ))

        # Add real choices (indices 1, 2, 3, ...)
        for i, c in enumerate(session.choices):
            list_view.append(ChoiceItem(
                c.get("label", "???"), c.get("summary", ""),
                index=i + 1, display_index=len(EXTRA_OPTIONS) + i,
            ))

        list_view.display = True
        # Restore scroll position or default to first real choice
        if session.scroll_index > 0:
            list_view.index = session.scroll_index
        else:
            list_view.index = len(EXTRA_OPTIONS)  # first real choice
        list_view.focus()

        if self._dwell_time > 0:
            dwell_bar = self.query_one("#dwell-bar", DwellBar)
            dwell_bar.dwell_time = self._dwell_time
            dwell_bar.progress = 0.0
            dwell_bar.display = True
            self._start_dwell()
        else:
            self.query_one("#dwell-bar").display = False

        # Update speech log
        self._update_speech_log()

    def _show_waiting(self, label: str) -> None:
        """Show waiting state after selection."""
        self.query_one("#choices").display = False
        self.query_one("#preamble").display = False
        self.query_one("#dwell-bar").display = False
        status = self.query_one("#status", Label)
        session = self._focused()
        session_name = session.name if session else ""
        after_text = f"Selected: {label}" if self._demo else f"[{session_name}] Selected: {label} — waiting for agent..."
        status.update(after_text)
        status.display = True

    def _show_idle(self) -> None:
        """Show idle state (no active choices, no agent connected)."""
        self.query_one("#choices").display = False
        self.query_one("#preamble").display = False
        self.query_one("#dwell-bar").display = False
        self.query_one("#speech-log").display = False
        status = self.query_one("#status", Label)
        status_text = "Ready — demo mode" if self._demo else "Waiting for agent..."
        status.update(status_text)
        status.display = True

    # ─── Speech with priority ─────────────────────────────────────

    def session_speak(self, session: Session, text: str, block: bool = True) -> None:
        """Speak text for a session, respecting priority rules.

        Foreground: plays immediately, interrupts background.
        Background: queued, played when foreground is idle.
        Always logs to session's speech_log.
        """
        # Log the speech
        entry = SpeechEntry(text=text)
        session.speech_log.append(entry)

        # Update speech log UI if this is the focused session
        if self._is_focused(session.session_id):
            try:
                self.call_from_thread(self._update_speech_log)
            except Exception:
                pass

        if self._is_focused(session.session_id):
            # Foreground: play immediately
            self._fg_speaking = True
            if block:
                self._tts.speak(text)
            else:
                self._tts.speak_async(text)
            self._fg_speaking = False
        else:
            # Background: queue
            entry.played = False
            session.unplayed_speech.append(entry)
            self._try_play_background_queue()

    def session_speak_async(self, session: Session, text: str) -> None:
        """Non-blocking speak for a session."""
        self.session_speak(session, text, block=False)

    def speak(self, text: str) -> None:
        """Legacy blocking TTS — uses focused session or plays directly."""
        session = self._focused()
        if session:
            self.session_speak(session, text, block=True)
        else:
            self._tts.speak(text)

    def speak_async(self, text: str) -> None:
        """Legacy non-blocking TTS — uses focused session."""
        session = self._focused()
        if session:
            self.session_speak(session, text, block=False)
        else:
            self._tts.speak_async(text)

    def _try_play_background_queue(self) -> None:
        """Try to play queued background speech if foreground is idle."""
        if self._fg_speaking:
            return

        # Find any session with unplayed speech
        for session in self.manager.all_sessions():
            if session.session_id == self.manager.active_session_id:
                continue  # skip foreground
            while session.unplayed_speech:
                if self._fg_speaking:
                    return  # foreground took over
                entry = session.unplayed_speech.pop(0)
                entry.played = True
                self._tts.speak(entry.text)  # blocking so we play in order

    # ─── Tab switching ─────────────────────────────────────────────

    def _switch_to_session(self, session: Session) -> None:
        """Switch UI to a different session. Called from main thread (action methods)."""
        # Save current scroll position
        old_session = self._focused()
        if old_session and old_session.session_id != session.session_id:
            try:
                list_view = self.query_one("#choices", ListView)
                old_session.scroll_index = list_view.index or 0
            except Exception:
                pass

        # Stop current TTS
        self._tts.stop()
        if old_session:
            old_session.reading_options = False

        # Focus new session
        self.manager.focus(session.session_id)

        # Update UI directly (we're on the main thread)
        self._update_tab_bar()

        if session.active:
            # Session has active choices — show them
            self._show_choices()

            # Play back unplayed speech then read prompt+options in bg thread
            def _play_inbox():
                while session.unplayed_speech:
                    entry = session.unplayed_speech.pop(0)
                    entry.played = True
                    self._fg_speaking = True
                    self._tts.speak(entry.text)
                    self._fg_speaking = False

                # Then read prompt + options
                if session.active:
                    numbered_labels = [
                        f"{i+1}. {c.get('label', '')}" for i, c in enumerate(session.choices)
                    ]
                    titles_readout = " ".join(numbered_labels)
                    full_intro = f"{session.preamble} Your options are: {titles_readout}"
                    self._fg_speaking = True
                    self._tts.speak(full_intro)
                    self._fg_speaking = False

                    # Read all options
                    session.reading_options = True
                    for i, c in enumerate(session.choices):
                        if not session.reading_options or not session.active:
                            break
                        s = c.get('summary', '')
                        text = f"{i+1}. {c.get('label', '')}. {s}" if s else f"{i+1}. {c.get('label', '')}"
                        self._fg_speaking = True
                        self._tts.speak(text)
                        self._fg_speaking = False
                    session.reading_options = False

            threading.Thread(target=_play_inbox, daemon=True).start()
        else:
            # No active choices — show speech log and waiting state
            self._update_speech_log()

            # Play unplayed speech in bg thread
            def _play_inbox_only():
                while session.unplayed_speech:
                    entry = session.unplayed_speech.pop(0)
                    entry.played = True
                    self._fg_speaking = True
                    self._tts.speak(entry.text)
                    self._fg_speaking = False

            if session.unplayed_speech:
                threading.Thread(target=_play_inbox_only, daemon=True).start()

            self._show_session_waiting(session)

    def _show_session_waiting(self, session: Session) -> None:
        """Show waiting state for a specific session."""
        self.query_one("#choices").display = False
        self.query_one("#preamble").display = False
        self.query_one("#dwell-bar").display = False
        self._update_speech_log()
        status = self.query_one("#status", Label)
        status.update(f"[{session.name}] Waiting for agent...")
        status.display = True

    def action_next_tab(self) -> None:
        """Switch to next tab."""
        session = self._focused()
        if session and (session.input_mode or session.voice_recording):
            return
        new_session = self.manager.next_tab()
        if new_session:
            self._tts.stop()
            self._tts.speak_async(new_session.name)
            self._switch_to_session(new_session)

    def action_prev_tab(self) -> None:
        """Switch to previous tab."""
        session = self._focused()
        if session and (session.input_mode or session.voice_recording):
            return
        new_session = self.manager.prev_tab()
        if new_session:
            self._tts.stop()
            self._tts.speak_async(new_session.name)
            self._switch_to_session(new_session)

    def action_next_choices_tab(self) -> None:
        """Cycle to next tab with active choices."""
        session = self._focused()
        if session and (session.input_mode or session.voice_recording):
            return
        new_session = self.manager.next_with_choices()
        if new_session:
            self._tts.stop()
            self._tts.speak_async(new_session.name)
            self._switch_to_session(new_session)
        else:
            self._tts.speak_async("No other tabs with choices")

    # ─── Session lifecycle ─────────────────────────────────────────

    def on_session_created(self, session: Session) -> None:
        """Called when a new session is created (from MCP thread)."""
        try:
            self.call_from_thread(self._update_tab_bar)
        except Exception:
            pass

    def on_session_removed(self, session_id: str) -> None:
        """Called when a session is removed."""
        new_active = self.manager.remove(session_id)
        try:
            self.call_from_thread(self._update_tab_bar)
            if new_active is not None:
                new_session = self.manager.get(new_active)
                if new_session:
                    self._switch_to_session(new_session)
            else:
                self.call_from_thread(self._show_idle)
        except Exception:
            pass

    # ─── Prompt replay ────────────────────────────────────────────

    def action_replay_prompt(self) -> None:
        """Replay just the preamble."""
        session = self._focused()
        if not session or not session.active or not session.preamble:
            return
        session.reading_options = False
        self._tts.stop()
        self._tts.speak_async(session.preamble)

    def action_replay_prompt_full(self) -> None:
        """Replay preamble + all options."""
        session = self._focused()
        if not session or not session.active:
            return
        session.reading_options = False
        self._tts.stop()

        def _replay():
            self._fg_speaking = True
            self._tts.speak(session.preamble)
            numbered_labels = [
                f"{i+1}. {c.get('label', '')}" for i, c in enumerate(session.choices)
            ]
            self._tts.speak("Your options are: " + " ".join(numbered_labels))
            session.reading_options = True
            for i, c in enumerate(session.choices):
                if not session.reading_options or not session.active:
                    break
                s = c.get('summary', '')
                text = f"{i+1}. {c.get('label', '')}. {s}" if s else f"{i+1}. {c.get('label', '')}"
                self._tts.speak(text)
            session.reading_options = False
            self._fg_speaking = False

        threading.Thread(target=_replay, daemon=True).start()

    # ─── Voice input ──────────────────────────────────────────────

    def action_voice_input(self) -> None:
        """Toggle voice recording mode."""
        session = self._focused()
        if not session or not session.active:
            return
        if session.voice_recording:
            self._stop_voice_recording()
        else:
            self._start_voice_recording()

    def _start_voice_recording(self) -> None:
        """Start recording audio via termux-microphone-record.

        Uses termux-exec to invoke termux-microphone-record in native Termux
        (outside proot) which has access to Android mic hardware. On stop,
        the recorded file is converted via ffmpeg and piped to stt --stdin.
        """
        session = self._focused()
        if not session:
            return
        session.voice_recording = True
        session.reading_options = False

        # Stop all audio immediately before mic opens
        self._tts.stop()

        # UI update
        self.query_one("#choices").display = False
        self.query_one("#dwell-bar").display = False
        status = self.query_one("#status", Label)
        status.update("🎙 Recording... (press space to stop)")
        status.display = True

        # Find binaries
        termux_exec_bin = _find_binary("termux-exec")
        stt_bin = _find_binary("stt")
        ffmpeg_bin = _find_binary("ffmpeg")

        if not termux_exec_bin:
            session.voice_recording = False
            self._tts.speak_async("termux-exec not found — cannot record audio")
            self._restore_choices()
            return

        if not stt_bin:
            session.voice_recording = False
            self._tts.speak_async("stt tool not found")
            self._restore_choices()
            return

        if not ffmpeg_bin:
            session.voice_recording = False
            self._tts.speak_async("ffmpeg not found")
            self._restore_choices()
            return

        # Record to shared storage (accessible from both native Termux and proot)
        rec_dir = "/sdcard/io-mcp"
        os.makedirs(rec_dir, exist_ok=True)
        self._voice_rec_file = os.path.join(rec_dir, "voice-recording.ogg")
        # Native Termux sees /storage/emulated/0 instead of /sdcard
        native_rec_file = "/storage/emulated/0/io-mcp/voice-recording.ogg"

        try:
            # Start recording via termux-exec (runs in native Termux context)
            self._voice_process = subprocess.Popen(
                [termux_exec_bin, "termux-microphone-record",
                 "-f", native_rec_file,
                 "-e", "opus", "-r", "24000", "-c", "1"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except Exception as e:
            session.voice_recording = False
            self._tts.speak_async(f"Voice input failed: {str(e)[:80]}")
            self._voice_process = None
            self._restore_choices()

    def _stop_voice_recording(self) -> None:
        """Stop recording and process transcription.

        Stops termux-microphone-record, then runs ffmpeg to convert the
        recorded opus file to raw PCM16 24kHz mono, piped into stt --stdin.
        """
        session = self._focused()
        if not session:
            return
        session.voice_recording = False
        proc = self._voice_process
        self._voice_process = None

        status = self.query_one("#status", Label)
        status.update("⏳ Transcribing...")

        def _process():
            termux_exec_bin = _find_binary("termux-exec")
            stt_bin = _find_binary("stt")
            ffmpeg_bin = _find_binary("ffmpeg")
            rec_file = getattr(self, '_voice_rec_file', None)

            # Stop the recording
            if termux_exec_bin:
                try:
                    subprocess.run(
                        [termux_exec_bin, "termux-microphone-record", "-q"],
                        timeout=5, capture_output=True,
                    )
                except Exception:
                    pass

            # Wait for the record process to finish
            if proc:
                try:
                    proc.wait(timeout=5)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass

            # Check file exists
            if not rec_file or not os.path.isfile(rec_file):
                self._tts.speak_async("No recording file found. Back to choices.")
                self.call_from_thread(self._restore_choices)
                return

            # Convert and transcribe: ffmpeg → stt --stdin
            env = os.environ.copy()
            env["PULSE_SERVER"] = os.environ.get("PULSE_SERVER", "127.0.0.1")
            env["LD_LIBRARY_PATH"] = PORTAUDIO_LIB

            try:
                # ffmpeg converts opus to raw PCM16 24kHz mono
                ffmpeg_proc = subprocess.Popen(
                    [ffmpeg_bin, "-y", "-i", rec_file,
                     "-f", "s16le", "-ar", "24000", "-ac", "1", "-"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                )
                # stt reads PCM16 from stdin
                stt_proc = subprocess.Popen(
                    [stt_bin, "--stdin"],
                    stdin=ffmpeg_proc.stdout,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env=env,
                )
                ffmpeg_proc.stdout.close()

                stdout, stderr = stt_proc.communicate(timeout=30)
                transcript = stdout.decode("utf-8", errors="replace").strip()
                stderr_text = stderr.decode("utf-8", errors="replace").strip() if stderr else ""
            except Exception as e:
                transcript = ""
                stderr_text = str(e)
            finally:
                # Clean up recording file
                try:
                    os.unlink(rec_file)
                except Exception:
                    pass

            if transcript:
                self._tts.stop()
                self._tts.speak_async(f"Got: {transcript}")

                wrapped = (
                    f"<transcription>\n{transcript}\n</transcription>\n"
                    "Note: This is a speech-to-text transcription that may contain "
                    "slight errors or similar-sounding words. Please interpret "
                    "charitably. If completely uninterpretable, present the same "
                    "options again and ask the user to retry."
                )
                session.selection = {"selected": wrapped, "summary": "(voice input)"}
                session.selection_event.set()
                self.call_from_thread(self._show_waiting, f"🎙 {transcript[:50]}")
            else:
                if stderr_text:
                    self._tts.speak_async(f"Recording failed: {stderr_text[:100]}")
                else:
                    self._tts.speak_async("No speech detected. Back to choices.")
                self.call_from_thread(self._restore_choices)

        threading.Thread(target=_process, daemon=True).start()

    def _restore_choices(self) -> None:
        """Restore the choices UI after voice/settings mode."""
        self.query_one("#status").display = False
        self.query_one("#choices").display = True
        list_view = self.query_one("#choices", ListView)
        list_view.focus()
        if self._dwell_time > 0:
            self.query_one("#dwell-bar").display = True
            self._start_dwell()

    # ─── Settings menu ────────────────────────────────────────────

    def action_toggle_settings(self) -> None:
        """Toggle settings menu. Always available regardless of agent connection."""
        session = self._focused()
        if session and session.in_settings:
            self._exit_settings()
            return
        self._enter_settings()

    def _enter_settings(self) -> None:
        """Show settings menu."""
        session = self._focused()
        if session:
            session.in_settings = True
            session.reading_options = False
        self._setting_edit_mode = False

        self._settings_items = [
            {"label": "Speed", "key": "speed",
             "summary": f"Current: {self.settings.speed:.1f}"},
            {"label": "Voice", "key": "voice",
             "summary": f"Current: {self.settings.voice}"},
            {"label": "Provider", "key": "provider",
             "summary": f"Current: {self.settings.provider}"},
            {"label": "Close settings", "key": "close", "summary": ""},
        ]

        preamble_widget = self.query_one("#preamble", Label)
        preamble_widget.update("Settings")
        preamble_widget.display = True

        list_view = self.query_one("#choices", ListView)
        list_view.clear()
        for i, s in enumerate(self._settings_items):
            summary = s.get("summary", "")
            list_view.append(ChoiceItem(s["label"], summary, index=i+1, display_index=i))
        list_view.display = True
        list_view.index = 0
        list_view.focus()

        # TTS after UI is updated
        self._tts.stop()
        self._tts.speak_async("Settings")

    def _exit_settings(self) -> None:
        """Leave settings and restore choices."""
        session = self._focused()
        if session:
            session.in_settings = False
        self._setting_edit_mode = False

        # UI first, then TTS
        if session and session.active:
            self._show_choices()
            self._tts.stop()
            self._tts.speak_async("Back to choices")
        else:
            self._show_idle()
            self._tts.stop()
            self._tts.speak_async("Settings closed")

    def _enter_setting_edit(self, key: str) -> None:
        """Enter edit mode for a specific setting."""
        self._setting_edit_mode = True
        self._setting_edit_key = key

        if key == "speed":
            self._setting_edit_values = [f"{v/10:.1f}" for v in range(5, 26)]
            current = f"{self.settings.speed:.1f}"
            self._setting_edit_index = (
                self._setting_edit_values.index(current)
                if current in self._setting_edit_values else 0
            )

        elif key == "voice":
            self._setting_edit_values = self.settings.get_voices()
            current = self.settings.voice
            self._setting_edit_index = (
                self._setting_edit_values.index(current)
                if current in self._setting_edit_values else 0
            )

        elif key == "provider":
            self._setting_edit_values = ["openai", "azure-speech"]
            current = self.settings.provider
            self._setting_edit_index = (
                self._setting_edit_values.index(current)
                if current in self._setting_edit_values else 0
            )

        # UI first
        list_view = self.query_one("#choices", ListView)
        list_view.clear()
        for i, val in enumerate(self._setting_edit_values):
            marker = " ✓" if i == self._setting_edit_index else ""
            list_view.append(ChoiceItem(f"{val}{marker}", "", index=i+1, display_index=i))
        list_view.index = self._setting_edit_index
        list_view.focus()

        # TTS after UI
        self._tts.stop()
        current_val = self._setting_edit_values[self._setting_edit_index]
        self._tts.speak_async(f"Editing {key}. Current: {current_val}. Scroll to change, Enter to confirm.")

        # Pregenerate in background
        if key in ("speed", "voice"):
            threading.Thread(
                target=self._tts.pregenerate, args=(self._setting_edit_values,), daemon=True
            ).start()

    def _apply_setting_edit(self) -> None:
        """Apply the current edit selection."""
        key = self._setting_edit_key
        list_view = self.query_one("#choices", ListView)
        idx = list_view.index or 0
        if idx >= len(self._setting_edit_values):
            idx = 0
        value = self._setting_edit_values[idx]

        if key == "speed":
            self.settings.speed = float(value)
        elif key == "voice":
            self.settings.voice = value
        elif key == "provider":
            self.settings.provider = value
            voices = self.settings.get_voices()
            if self.settings.voice not in voices:
                self.settings.voice = voices[0]

        self.settings.apply_to_env()
        self._tts.clear_cache()

        self._setting_edit_mode = False
        self._tts.stop()
        self._tts.speak_async(f"{key} set to {value}")

        self._enter_settings()

    # ─── Dwell timer ─────────────────────────────────────────────

    def _start_dwell(self) -> None:
        self._cancel_dwell()
        self._dwell_start = time.time()
        self._dwell_timer = self.set_interval(0.05, self._tick_dwell)

    def _cancel_dwell(self) -> None:
        if self._dwell_timer is not None:
            self._dwell_timer.stop()
            self._dwell_timer = None

    def _tick_dwell(self) -> None:
        session = self._focused()
        if not session or not session.active or self._dwell_time <= 0:
            self._cancel_dwell()
            return
        elapsed = time.time() - self._dwell_start
        progress = min(1.0, elapsed / self._dwell_time)
        dwell_bar = self.query_one("#dwell-bar", DwellBar)
        dwell_bar.progress = progress
        if progress >= 1.0:
            self._cancel_dwell()
            self._do_select()

    # ─── Event handlers ──────────────────────────────────────────

    @on(ListView.Highlighted)
    def on_highlight_changed(self, event: ListView.Highlighted) -> None:
        """Speak label + description when highlight changes."""
        if event.item is None:
            return

        session = self._focused()

        # In setting edit mode, read the value
        if self._setting_edit_mode:
            if isinstance(event.item, ChoiceItem):
                val = self._setting_edit_values[event.item.display_index] if event.item.display_index < len(self._setting_edit_values) else ""
                self._tts.speak_async(val)
            return

        # In settings mode
        if session and session.in_settings:
            if isinstance(event.item, ChoiceItem):
                s = self._settings_items[event.item.display_index] if event.item.display_index < len(self._settings_items) else None
                if s:
                    text = f"{s['label']}. {s.get('summary', '')}" if s.get('summary') else s['label']
                    self._tts.speak_async(text)
            return

        if not session or not session.active:
            return
        if session.intro_speaking:
            return

        # If we're reading options sequentially and user scrolled, interrupt
        if session.reading_options:
            session.reading_options = False
            self._tts.stop()

        if isinstance(event.item, ChoiceItem):
            logical = event.item.choice_index
            if logical > 0:
                ci = logical - 1
                c = session.choices[ci]
                s = c.get('summary', '')
                text = f"{logical}. {c.get('label', '')}. {s}" if s else f"{logical}. {c.get('label', '')}"
            else:
                ei = len(EXTRA_OPTIONS) - 1 + logical
                if 0 <= ei < len(EXTRA_OPTIONS):
                    e = EXTRA_OPTIONS[ei]
                    text = f"{e['label']}. {e.get('summary', '')}" if e.get('summary') else e['label']
                else:
                    text = ""
            if text:
                self._tts.speak_async(text)

            if self._dwell_time > 0:
                self._start_dwell()

    @on(ListView.Selected)
    def on_list_selected(self, event: ListView.Selected) -> None:
        """Handle Enter/click on a list item."""
        session = self._focused()
        if self._setting_edit_mode:
            self._apply_setting_edit()
            return
        if session and session.in_settings:
            if isinstance(event.item, ChoiceItem):
                idx = event.item.display_index
                if idx < len(self._settings_items):
                    key = self._settings_items[idx]["key"]
                    if key == "close":
                        self._exit_settings()
                    else:
                        self._enter_setting_edit(key)
            return
        if not session or not session.active:
            return
        self._do_select()

    def action_cursor_down(self) -> None:
        session = self._focused()
        if session and (session.input_mode or session.voice_recording):
            return
        list_view = self.query_one("#choices", ListView)
        if list_view.display:
            list_view.action_cursor_down()

    def action_cursor_up(self) -> None:
        session = self._focused()
        if session and (session.input_mode or session.voice_recording):
            return
        list_view = self.query_one("#choices", ListView)
        if list_view.display:
            list_view.action_cursor_up()

    def _scroll_allowed(self) -> bool:
        """Check if enough time has passed since the last scroll."""
        now = time.time()
        if now - self._last_scroll_time < self._scroll_debounce:
            return False
        self._last_scroll_time = now
        return True

    def on_mouse_scroll_down(self, event: MouseScrollDown) -> None:
        session = self._focused()
        if session and (session.active or session.in_settings or self._setting_edit_mode) and self._scroll_allowed():
            list_view = self.query_one("#choices", ListView)
            if list_view.display:
                if self._invert_scroll:
                    list_view.action_cursor_up()
                else:
                    list_view.action_cursor_down()
                event.prevent_default()
                event.stop()

    def on_mouse_scroll_up(self, event: MouseScrollUp) -> None:
        session = self._focused()
        if session and (session.active or session.in_settings or self._setting_edit_mode) and self._scroll_allowed():
            list_view = self.query_one("#choices", ListView)
            if list_view.display:
                if self._invert_scroll:
                    list_view.action_cursor_down()
                else:
                    list_view.action_cursor_up()
                event.prevent_default()
                event.stop()

    def action_select(self) -> None:
        if self._setting_edit_mode:
            self._apply_setting_edit()
            return
        session = self._focused()
        if session and session.in_settings:
            list_view = self.query_one("#choices", ListView)
            idx = list_view.index or 0
            if idx < len(self._settings_items):
                key = self._settings_items[idx]["key"]
                if key == "close":
                    self._exit_settings()
                else:
                    self._enter_setting_edit(key)
            return
        if session and session.active and not session.input_mode and not session.voice_recording:
            self._do_select()

    def action_freeform_input(self) -> None:
        """Switch to freeform text input mode."""
        session = self._focused()
        if not session or not session.active or session.input_mode or session.voice_recording:
            return
        session.input_mode = True
        self._freeform_spoken_pos = 0
        session.reading_options = False
        self._cancel_dwell()

        # UI first
        self.query_one("#choices").display = False
        self.query_one("#dwell-bar").display = False
        inp = self.query_one("#freeform-input", Input)
        inp.value = ""
        inp.styles.display = "block"
        inp.focus()

        # TTS after UI
        self._tts.stop()
        self._tts.speak_async("Type your reply")

    @on(Input.Changed, "#freeform-input")
    def on_freeform_changed(self, event: Input.Changed) -> None:
        session = self._focused()
        if not session or not session.input_mode:
            return
        text = event.value
        if len(text) <= self._freeform_spoken_pos:
            self._freeform_spoken_pos = len(text)
            return
        if text and text[-1] in self._freeform_delimiters:
            chunk = text[self._freeform_spoken_pos:].strip()
            if chunk:
                self._freeform_tts.stop()
                self._freeform_tts.speak_async(chunk)
            self._freeform_spoken_pos = len(text)

    @on(Input.Submitted, "#freeform-input")
    def on_freeform_submitted(self, event: Input.Submitted) -> None:
        session = self._focused()
        if not session:
            return
        text = event.value.strip()
        if not text:
            return
        session.input_mode = False
        event.input.styles.display = "none"

        self._freeform_tts.stop()
        self._tts.stop()
        self._tts.speak_async(f"Selected: {text}")

        session.selection = {"selected": text, "summary": "(freeform input)"}
        session.selection_event.set()
        self._show_waiting(text)

    def _cancel_freeform(self) -> None:
        session = self._focused()
        if session:
            session.input_mode = False
        self._freeform_tts.stop()
        inp = self.query_one("#freeform-input", Input)
        inp.styles.display = "none"
        self._restore_choices()
        self._tts.speak_async("Cancelled. Back to choices.")

    def on_key(self, event) -> None:
        """Handle Escape in freeform/voice/settings mode."""
        session = self._focused()
        if session and session.input_mode and event.key == "escape":
            self._cancel_freeform()
            event.prevent_default()
            event.stop()
        elif session and session.voice_recording and event.key == "escape":
            # Kill recording process and stop termux-microphone-record
            if self._voice_process:
                try:
                    self._voice_process.kill()
                except Exception:
                    pass
            termux_exec_bin = _find_binary("termux-exec")
            if termux_exec_bin:
                try:
                    subprocess.run(
                        [termux_exec_bin, "termux-microphone-record", "-q"],
                        timeout=3, capture_output=True,
                    )
                except Exception:
                    pass
            session.voice_recording = False
            self._voice_process = None
            # Clean up recording file
            rec_file = getattr(self, '_voice_rec_file', None)
            if rec_file:
                try:
                    os.unlink(rec_file)
                except Exception:
                    pass
            self._tts.speak_async("Recording cancelled")
            self._restore_choices()
            event.prevent_default()
            event.stop()
        elif self._setting_edit_mode and event.key == "escape":
            self._setting_edit_mode = False
            self._enter_settings()
            event.prevent_default()
            event.stop()
        elif session and session.in_settings and event.key == "escape":
            self._exit_settings()
            event.prevent_default()
            event.stop()

    def _pick_by_number(self, n: int) -> None:
        """Immediately select option by 1-based number."""
        session = self._focused()
        if not session or not session.active or session.input_mode or session.voice_recording or session.in_settings:
            return
        display_idx = session.extras_count + n - 1
        list_view = self.query_one("#choices", ListView)
        if display_idx < 0 or display_idx >= len(list_view.children):
            return
        list_view.index = display_idx
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
        # Set quit for all active sessions
        for session in self.manager.all_sessions():
            if session.active:
                self._cancel_dwell()
                session.selection = {"selected": "quit", "summary": "User quit"}
                session.selection_event.set()
        self.exit()

    def _do_select(self) -> None:
        """Finalize the current selection."""
        session = self._focused()
        if not session or not session.active or not session.choices:
            return
        self._cancel_dwell()
        session.reading_options = False

        list_view = self.query_one("#choices", ListView)
        idx = list_view.index or 0
        item = self._get_item_at_display_index(idx)
        if item is None:
            return

        logical = item.choice_index

        # Handle extra options
        if logical <= 0:
            self._handle_extra_select(logical)
            return

        # Real choice
        ci = logical - 1
        if ci >= len(session.choices):
            ci = 0
        chosen = session.choices[ci]
        label = chosen.get("label", "")
        summary = chosen.get("summary", "")

        self._tts.stop()
        self._tts.speak_async(f"Selected: {label}")

        session.selection = {"selected": label, "summary": summary}
        session.selection_event.set()
        self._show_waiting(label)

    def _handle_extra_select(self, logical_index: int) -> None:
        """Handle selection of extra options.

        Display order (top to bottom): -3=Record, -2=Fast, -1=Voice, 0=Settings.
        Maps logical_index to EXTRA_OPTIONS array via: ei = len(EXTRA_OPTIONS) - 1 + logical_index.
        """
        self._tts.stop()

        ei = len(EXTRA_OPTIONS) - 1 + logical_index
        if ei < 0 or ei >= len(EXTRA_OPTIONS):
            return

        label = EXTRA_OPTIONS[ei]["label"]
        if label == "Record response":
            self.action_voice_input()
        elif label == "Next tab":
            self.action_next_tab()
        elif label == "Previous tab":
            self.action_prev_tab()
        elif label == "Fast toggle":
            msg = self.settings.toggle_fast()
            self._tts.clear_cache()
            self._tts.speak_async(msg)
        elif label == "Voice toggle":
            msg = self.settings.toggle_voice()
            self._tts.clear_cache()
            self._tts.speak_async(msg)
        elif label == "Settings":
            self._enter_settings()


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
        if self._app is not None:
            try:
                self._app.exit()
            except Exception:
                pass
        self._tts.cleanup()

    def present_choices(self, preamble: str, choices: list[dict]) -> dict:
        if self._app is None:
            return {"selected": "error", "summary": "TUI not started"}
        # Legacy: create a default session
        session, _ = self._app.manager.get_or_create(0)
        return self._app.present_choices(session, preamble, choices)

    def speak(self, text: str) -> None:
        self._tts.speak(text)
