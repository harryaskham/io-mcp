"""Main TUI application for io-mcp.

Contains the IoMcpApp (Textual App subclass) and TUI controller wrapper.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
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

from ..session import Session, SessionManager, SpeechEntry, HistoryEntry
from ..settings import Settings
from ..tts import PORTAUDIO_LIB, TTSEngine, _find_binary
from .. import api as frontend_api

from .themes import COLOR_SCHEMES, DEFAULT_SCHEME, get_scheme, build_css
from .widgets import ChoiceItem, DwellBar, EXTRA_OPTIONS, _safe_action

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from ..config import IoMcpConfig


# Alias for internal use
_build_css = build_css


# ─── Main TUI App ───────────────────────────────────────────────────────────

class IoMcpApp(App):
    """Textual app for io-mcp choice presentation with multi-session support."""

    CSS = _build_css(DEFAULT_SCHEME)

    BINDINGS = [
        Binding("j,down", "cursor_down", "Down", show=False),
        Binding("k,up", "cursor_up", "Up", show=False),
        Binding("enter", "select", "Select", show=True),
        Binding("i", "freeform_input", "Type reply", show=True),
        Binding("m", "queue_message", "Message", show=True),
        Binding("space", "voice_input", "Voice", show=True),
        Binding("s", "toggle_settings", "Settings", show=True),
        Binding("p", "replay_prompt", "Replay", show=False),
        Binding("P", "replay_prompt_full", "Replay all", show=False),
        Binding("l", "next_tab", "Next tab", show=False),
        Binding("h", "prev_tab", "Prev tab", show=False),
        Binding("n", "next_choices_tab", "Next choices", show=False),
        Binding("u", "undo_selection", "Undo", show=False),
        Binding("slash", "filter_choices", "Filter", show=False),
        Binding("t", "spawn_agent", "New agent", show=False),
        Binding("x", "quick_actions", "Quick", show=False),
        Binding("c", "toggle_conversation", "Chat", show=False),
        Binding("d", "dashboard", "Dashboard", show=False),
        Binding("r", "hot_reload", "Reload", show=False),
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
        config: Optional["IoMcpConfig"] = None,
        **kwargs,
    ) -> None:
        # Build key bindings from config before super().__init__
        kb = config.key_bindings if config else {}
        down_key = kb.get("cursorDown", "j")
        up_key = kb.get("cursorUp", "k")
        select_key = kb.get("select", "enter")
        voice_key = kb.get("voiceInput", "space")
        freeform_key = kb.get("freeformInput", "i")
        message_key = kb.get("queueMessage", "m")
        settings_key = kb.get("settings", "s")
        replay_key = kb.get("replayPrompt", "p")
        replay_all_key = kb.get("replayAll", "P")
        next_tab_key = kb.get("nextTab", "l")
        prev_tab_key = kb.get("prevTab", "h")
        next_choices_key = kb.get("nextChoicesTab", "n")
        undo_key = kb.get("undoSelection", "u")
        filter_key = kb.get("filterChoices", "slash")
        spawn_key = kb.get("spawnAgent", "t")
        quick_key = kb.get("quickActions", "x")
        convo_key = kb.get("conversationMode", "c")
        dashboard_key = kb.get("dashboard", "d")
        log_key = kb.get("agentLog", "g")
        help_key = kb.get("help", "question_mark")
        reload_key = kb.get("hotReload", "r")
        quit_key = kb.get("quit", "q")

        self._bindings = [
            Binding(f"{down_key},down", "cursor_down", "Down", show=False),
            Binding(f"{up_key},up", "cursor_up", "Up", show=False),
            Binding(select_key, "select", "Select", show=True),
            Binding(freeform_key, "freeform_input", "Type reply", show=True),
            Binding(message_key, "queue_message", "Message", show=True),
            Binding(voice_key, "voice_input", "Voice", show=True),
            Binding(settings_key, "toggle_settings", "Settings", show=True),
            Binding(replay_key, "replay_prompt", "Replay", show=False),
            Binding(replay_all_key, "replay_prompt_full", "Replay all", show=False),
            Binding(next_tab_key, "next_tab", "Next tab", show=False),
            Binding(prev_tab_key, "prev_tab", "Prev tab", show=False),
            Binding(next_choices_key, "next_choices_tab", "Next choices", show=False),
            Binding(undo_key, "undo_selection", "Undo", show=False),
            Binding(filter_key, "filter_choices", "Filter", show=False),
            Binding(spawn_key, "spawn_agent", "New agent", show=False),
            Binding(quick_key, "quick_actions", "Quick", show=False),
            Binding(convo_key, "toggle_conversation", "Chat", show=False),
            Binding(dashboard_key, "dashboard", "Dashboard", show=False),
            Binding(log_key, "agent_log", "Log", show=False),
            Binding(help_key, "show_help", "Help", show=False),
            Binding(reload_key, "hot_reload", "Reload", show=False),
        ] + [Binding(str(i), f"pick_{i}", "", show=False) for i in range(1, 10)]
        if quit_key:
            self._bindings.append(Binding(quit_key, "quit", "Quit", show=False))

        # Apply color scheme from config
        scheme_name = DEFAULT_SCHEME
        if config:
            scheme_name = config.expanded.get("config", {}).get("colorScheme", DEFAULT_SCHEME)
        if scheme_name not in COLOR_SCHEMES:
            scheme_name = DEFAULT_SCHEME
        self.__class__.CSS = _build_css(scheme_name)
        self._color_scheme = scheme_name
        self._cs = get_scheme(scheme_name)  # shortcut for inline Rich markup

        super().__init__(**kwargs)
        self._tts = tts
        self._freeform_tts = freeform_tts or tts
        self._freeform_delimiters = set(freeform_delimiters)
        self._scroll_debounce = scroll_debounce
        self._invert_scroll = invert_scroll
        self._demo = demo
        self._config = config
        self._last_scroll_time: float = 0.0
        self._dwell_time = dwell_time

        # Session manager
        self.manager = SessionManager()

        # Freeform text input
        self._freeform_spoken_pos = 0

        # Voice input
        self._voice_process: Optional[subprocess.Popen] = None
        self._voice_rec_file: Optional[str] = None

        # Message queue mode
        self._message_mode = False

        # Settings (global, not per-session)
        self.settings = Settings(config=config)
        self._settings_items: list[dict] = []
        self._setting_edit_mode = False
        self._setting_edit_values: list[str] = []
        self._setting_edit_index: int = 0
        self._setting_edit_key: str = ""

        # Settings state (app-level, not per-session)
        self._in_settings = False
        self._settings_just_closed = False

        # Dwell timer
        self._dwell_timer: Optional[Timer] = None
        self._dwell_start: float = 0.0

        # Flag: is foreground currently speaking (blocks bg playback)
        self._fg_speaking = False

        # Haptic feedback
        self._termux_vibrate = _find_binary("termux-vibrate")
        self._haptic_enabled = self._termux_vibrate is not None

        # TTS deduplication — track last spoken text to avoid repeats
        self._last_spoken_text: str = ""

        # Filter mode
        self._filter_mode = False

        # Conversation mode — continuous voice back-and-forth
        self._conversation_mode = False

        # Log viewer mode
        self._log_viewer_mode = False

        # Help screen mode
        self._help_mode = False

        # Tab picker mode
        self._tab_picker_mode = False

        # Multi-select mode (toggle choices then confirm)
        self._multi_select_mode = False
        self._multi_select_checked: list[bool] = []

    # ─── Helpers to get focused session ────────────────────────────

    def _focused(self) -> Optional[Session]:
        """Get the currently focused session."""
        return self.manager.focused()

    def _is_focused(self, session_id: str) -> bool:
        """Check if a session is the focused one."""
        return self.manager.active_session_id == session_id

    # ─── Haptic feedback ────────────────────────────────────────────

    def _vibrate(self, duration_ms: int = 30) -> None:
        """Trigger haptic feedback via termux-vibrate (fire-and-forget).

        Uses termux-exec if available (needed on Nix-on-Droid/proot),
        otherwise falls back to direct termux-vibrate.

        Args:
            duration_ms: Vibration duration in milliseconds.
                         30ms for scroll, 100ms for selection.
        """
        if not self._haptic_enabled:
            return
        try:
            cmd: list[str] = []
            termux_exec = _find_binary("termux-exec")
            if termux_exec:
                cmd = [termux_exec, "termux-vibrate", "-d", str(duration_ms), "-f"]
            elif self._termux_vibrate:
                cmd = [self._termux_vibrate, "-d", str(duration_ms), "-f"]
            else:
                return
            subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            pass  # Don't let vibration errors affect UX

    # ─── Widget composition ────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield Header(name="io-mcp", show_clock=False)
        yield Static("", id="tab-bar")
        status_text = "[dim]Ready — demo mode[/dim]" if self._demo else "[dim]Waiting for agent...[/dim]"
        yield Label(status_text, id="status")
        yield Label("", id="agent-activity")
        yield Label("", id="preamble")
        yield Vertical(id="speech-log")
        yield ListView(id="choices")
        yield Input(placeholder="Type your reply, press Enter to send, Escape to cancel", id="freeform-input")
        yield Input(placeholder="Filter choices...", id="filter-input")
        yield DwellBar(id="dwell-bar")
        yield Static("[dim]↕[/dim] Scroll  [dim]⏎[/dim] Select  [dim]u[/dim] Undo  [dim]i[/dim] Type  [dim]m[/dim] Msg  [dim]␣[/dim] Voice  [dim]/[/dim] Filter  [dim]s[/dim] Settings  [dim]q[/dim] Quit", id="footer-help")

    def on_mount(self) -> None:
        self.title = "io-mcp"
        self.sub_title = ""
        self.query_one("#tab-bar").display = False
        self.query_one("#preamble").display = False
        self.query_one("#choices").display = False
        self.query_one("#dwell-bar").display = False
        self.query_one("#speech-log").display = False

        # Start periodic session cleanup (every 60 seconds, 5 min timeout)
        self._cleanup_timer = self.set_interval(60, self._cleanup_stale_sessions)
        # Heartbeat: check every 15s if agent has been silent too long
        self._heartbeat_timer = self.set_interval(15, self._check_heartbeat)

    def _touch_session(self, session: Session) -> None:
        """Update last_activity, safe for old Session objects without the field."""
        try:
            session.last_activity = time.time()
        except AttributeError:
            pass

    def _cleanup_stale_sessions(self) -> None:
        """Remove sessions that have been inactive past the configured timeout.

        Only removes non-focused sessions without active choices.
        Updates the tab bar if any sessions were removed.
        """
        if not hasattr(self.manager, 'cleanup_stale'):
            return
        timeout = 300.0
        if self._config and hasattr(self._config, 'session_cleanup_timeout'):
            timeout = self._config.session_cleanup_timeout
        removed = self.manager.cleanup_stale(timeout_seconds=timeout)
        if removed:
            self._update_tab_bar()

    def _check_heartbeat(self) -> None:
        """Ambient mode: speak escalating status updates during agent silence.

        Tracks elapsed time since the last MCP tool call and speaks
        progressively more informative updates:

        1st: "Agent is still working..."
        2nd+: "Still working, N minutes in. Last update: [text]"

        Configurable via config.ambient.{enabled, initialDelaySecs, repeatIntervalSecs}.
        Resets when the agent makes its next MCP tool call.
        """
        import time as _time

        # Check if ambient mode is enabled
        if self._config and not self._config.ambient_enabled:
            return

        session = self._focused()
        if not session:
            return
        # Only for connected sessions (has had at least one tool call)
        last_call = getattr(session, 'last_tool_call', 0)
        if last_call == 0:
            return
        # Don't speak during active choices (agent is waiting for user)
        if session.active:
            return

        elapsed = _time.time() - last_call
        initial_delay = self._config.ambient_initial_delay if self._config else 30
        repeat_interval = self._config.ambient_repeat_interval if self._config else 45
        ambient_count = getattr(session, 'ambient_count', 0)

        if ambient_count == 0:
            # First ambient update: after initial delay
            if elapsed >= initial_delay:
                session.ambient_count = 1
                self._tts.speak_async("Agent is still working...")
                self._update_ambient_indicator(session, elapsed)
        else:
            # Subsequent updates: after repeat interval from last update
            next_time = initial_delay + (ambient_count * repeat_interval)
            if elapsed >= next_time:
                session.ambient_count = ambient_count + 1
                minutes = int(elapsed) // 60
                last_text = ""
                if session.speech_log:
                    last_text = session.speech_log[-1].text
                    if len(last_text) > 60:
                        last_text = last_text[:60]

                last_tool = getattr(session, 'last_tool_name', '')
                tool_hint = f" Last tool: {last_tool}." if last_tool else ""

                if minutes >= 1 and last_text:
                    msg = f"Still working, {minutes} {'minute' if minutes == 1 else 'minutes'} in.{tool_hint} Last update: {last_text}"
                elif minutes >= 1:
                    msg = f"Still working, {minutes} {'minute' if minutes == 1 else 'minutes'} in.{tool_hint}"
                elif last_text:
                    msg = f"Still working.{tool_hint} Last update: {last_text}"
                else:
                    msg = f"Agent is still working...{tool_hint}"

                self._tts.speak_async(msg)
                self._update_ambient_indicator(session, elapsed)

    def _update_ambient_indicator(self, session: Session, elapsed: float) -> None:
        """Update the agent activity label with elapsed time and last tool."""
        try:
            activity = self.query_one("#agent-activity", Label)
            minutes = int(elapsed) // 60
            secs = int(elapsed) % 60
            if minutes > 0:
                time_str = f"{minutes}m{secs:02d}s"
            else:
                time_str = f"{secs}s"

            # Show last tool name if available
            last_tool = getattr(session, 'last_tool_name', '')
            tool_info = f" [{self._cs['purple']}]{last_tool}[/{self._cs['purple']}]" if last_tool else ""

            last_text = ""
            if session.speech_log:
                last_text = session.speech_log[-1].text
                if len(last_text) > 60:
                    last_text = last_text[:60] + "..."
            if last_text:
                activity.update(f"[bold {self._cs['warning']}]⧗[/bold {self._cs['warning']}] Working ({time_str}){tool_info} — {last_text}")
            else:
                activity.update(f"[bold {self._cs['warning']}]⧗[/bold {self._cs['warning']}] Working ({time_str}){tool_info}")
            activity.display = True
        except Exception:
            pass

    # ─── Tab bar rendering ─────────────────────────────────────────

    def _update_tab_bar(self) -> None:
        """Update the tab bar display."""
        tab_bar = self.query_one("#tab-bar", Static)
        if self.manager.count() <= 0:
            tab_bar.display = False
            return
        s = get_scheme(getattr(self, '_color_scheme', DEFAULT_SCHEME))
        tab_bar.update(self.manager.tab_bar_text(accent=s['accent'], success=s['success']))
        tab_bar.display = True

    # ─── Speech log rendering ──────────────────────────────────────

    def _update_speech_log(self) -> None:
        """Update the speech log display and agent activity indicator."""
        log_widget = self.query_one("#speech-log", Vertical)
        log_widget.remove_children()

        # Agent activity label (may not exist on hot-reloaded instances)
        try:
            activity = self.query_one("#agent-activity", Label)
        except Exception:
            activity = None

        session = self._focused()
        if session is None:
            log_widget.display = False
            if activity:
                activity.display = False
            return

        # Update agent activity line (most recent speech, truncated)
        if activity and session.speech_log:
            last = session.speech_log[-1].text
            truncated = last[:80] + ("..." if len(last) > 80 else "")
            activity.update(f"[bold {self._cs['blue']}]▸[/bold {self._cs['blue']}] {truncated}")
            activity.display = True
        elif activity:
            activity.display = False

        # Show last 5 speech entries
        recent = session.speech_log[-5:]
        if not recent:
            log_widget.display = False
            return

        for entry in recent:
            label = Label(f"[dim]  │[/dim] {entry.text}", classes="speech-entry")
            log_widget.mount(label)
        log_widget.display = True

    # ─── Choice presentation (called from MCP server thread) ─────────

    def present_choices(self, session: Session, preamble: str, choices: list[dict]) -> dict:
        """Show choices and block until user selects. Thread-safe.

        Each session has its own selection_event so multiple sessions
        can block independently.

        In conversation mode: speaks just the preamble, then auto-starts
        voice recording. The transcription becomes the selection.
        """
        try:
            return self._present_choices_inner(session, preamble, choices)
        except Exception as exc:
            import traceback
            err = f"{type(exc).__name__}: {str(exc)[:200]}"
            try:
                with open("/tmp/io-mcp-tui-error.log", "a") as f:
                    f.write(f"\n--- present_choices ---\n{traceback.format_exc()}\n")
            except Exception:
                pass
            # Speak the error so the user hears it
            try:
                self._tts.speak_async(f"Choice presentation error: {str(exc)[:80]}")
            except Exception:
                pass
            # Return error to agent so it has context
            return {"selected": "error", "summary": f"TUI error: {err}"}

    def _present_choices_inner(self, session: Session, preamble: str, choices: list[dict]) -> dict:
        """Inner implementation of present_choices."""
        self._touch_session(session)
        session.preamble = preamble
        session.choices = list(choices)
        session.selection = None
        session.selection_event.clear()
        session.active = True
        session.intro_speaking = True
        session.reading_options = False
        session.in_settings = False
        self._last_spoken_text = ""  # Reset dedup for new choices

        # Emit event for remote frontends
        try:
            frontend_api.emit_choices_presented(session.session_id, preamble, choices)
        except Exception:
            pass

        is_fg = self._is_focused(session.session_id)

        # ── Conversation mode: speak preamble then auto-record ──
        if self._conversation_mode and is_fg:
            session.intro_speaking = False
            session.reading_options = False

            # Audio cue
            self._tts.play_chime("choices")

            # Show conversation UI
            def _show_convo():
                self.query_one("#choices").display = False
                self.query_one("#dwell-bar").display = False
                preamble_widget = self.query_one("#preamble", Label)
                preamble_widget.update(f"[bold {self._cs['success']}]🗣[/bold {self._cs['success']}] {preamble}")
                preamble_widget.display = True
                status = self.query_one("#status", Label)
                status.update(f"[dim]Conversation mode[/dim] [{self._cs['blue']}](c to exit)[/{self._cs['blue']}]")
                status.display = True
            self.call_from_thread(_show_convo)

            # Speak preamble only (no options readout)
            self._fg_speaking = True
            self._tts.speak(preamble)
            self._fg_speaking = False

            # Auto-start voice recording after a brief pause
            import time as _time
            _time.sleep(0.3)

            # Check if conversation mode is still active (user might have pressed c)
            if self._conversation_mode and session.active:
                self.call_from_thread(self._start_voice_recording)

            # Block until selection (voice recording will set it)
            session.selection_event.wait()
            session.active = False

            # Reset ambient timer
            import time as _time_mod
            session.last_tool_call = _time_mod.time()
            session.ambient_count = 0

            self.call_from_thread(self._update_tab_bar)
            return session.selection or {"selected": "timeout", "summary": ""}

        # ── Normal mode: full choice presentation ──
        # Build the full list: extras + real choices
        session.extras_count = len(EXTRA_OPTIONS)
        session.all_items = list(EXTRA_OPTIONS) + session.choices

        # Build TTS texts (skip silent options in intro readout)
        numbered_labels = []
        numbered_full_all = []
        for i, c in enumerate(choices):
            is_silent = c.get('_silent', False)
            label_text = f"{i+1}. {c.get('label', '')}"
            s = c.get('summary', '')
            full_text = f"{i+1}. {c.get('label', '')}. {s}" if s else label_text

            if not is_silent:
                numbered_labels.append(label_text)
            numbered_full_all.append(full_text)

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
            # Audio cue for new choices
            self._tts.play_chime("choices")

            # Foreground: speak intro and read options
            self._fg_speaking = True
            self._tts.speak(full_intro)

            session.intro_speaking = False
            # Only read options if intro wasn't interrupted by scrolling
            if session.active and not session.selection:
                session.reading_options = True
                for i, text in enumerate(numbered_full_all):
                    if not session.reading_options or not session.active:
                        break
                    # Skip silent options in the readout
                    if i < len(choices) and choices[i].get('_silent', False):
                        continue
                    self._tts.speak(text)
                session.reading_options = False
            self._fg_speaking = False

            # Don't re-read the current highlight after intro — it was just read
            # The user can scroll to trigger readout of individual items

            # Try playing any background queued speech
            self._try_play_background_queue()
        else:
            # Background: queue intro for later, read abbreviated version
            session.intro_speaking = False
            entry = SpeechEntry(text=full_intro)
            session.unplayed_speech.append(entry)
            session.speech_log.append(SpeechEntry(text=f"[choices] {preamble}"))

            # Alert: chime + speak session name so user knows which tab needs attention
            self._tts.play_chime("choices")
            self._tts.speak_async(f"{session.name} has choices")

            # Try to speak in background if fg is idle
            self._try_play_background_queue()

        # Block until selection
        session.selection_event.wait()
        session.active = False

        # Reset ambient timer — selection counts as activity
        import time as _time_mod
        session.last_tool_call = _time_mod.time()
        session.ambient_count = 0

        self.call_from_thread(self._update_tab_bar)

        return session.selection or {"selected": "timeout", "summary": ""}

    def present_multi_select(self, session: Session, preamble: str, choices: list[dict]) -> list[dict]:
        """Show choices with toggleable checkboxes. Returns list of selected items.

        Uses the same UI as present_choices but:
        - Labels are prefixed with [ ] or [✓] to show checked state
        - Enter toggles the current item instead of selecting
        - A "Done" item at the end submits all checked items
        """
        self._touch_session(session)
        checked = [False] * len(choices)

        # Add "Done" as the last choice
        done_label = "✅ Done — submit selections"
        augmented = list(choices) + [{"label": done_label, "summary": "Submit all checked items"}]

        def _make_labels():
            """Build choice labels with checkbox state."""
            result = []
            for i, c in enumerate(choices):
                prefix = "✓" if checked[i] else "○"
                result.append({
                    "label": f"[{prefix}] {c.get('label', '')}",
                    "summary": c.get("summary", ""),
                })
            result.append({"label": done_label, "summary": f"{sum(checked)} item(s) selected"})
            return result

        while True:
            labeled = _make_labels()
            result = self.present_choices(session, preamble, labeled)
            selected = result.get("selected", "")

            if selected == done_label or selected == "quit":
                break

            # Find which item was toggled
            for i, c in enumerate(choices):
                check_label = f"[✓] {c.get('label', '')}"
                uncheck_label = f"[○] {c.get('label', '')}"
                if selected in (check_label, uncheck_label):
                    checked[i] = not checked[i]
                    state = "checked" if checked[i] else "unchecked"
                    self._tts.speak_async(f"{c.get('label', '')} {state}")
                    break

        # Return all checked items
        return [choices[i] for i in range(len(choices)) if checked[i]]

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

        # Don't overwrite the UI if user is composing a message or typing
        if self._message_mode or (session and session.input_mode):
            # Choices are stored on the session; they'll be shown after input is done
            # Just play a subtle chime to indicate choices arrived
            self._tts.play_chime("choices")
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
        after_text = f"Selected: {label}" if self._demo else f"[{self._cs['success']}]✓[/{self._cs['success']}] [{session_name}] {label} [dim](u=undo)[/dim]"
        status.update(after_text)
        status.display = True

    def _show_idle(self) -> None:
        """Show idle state (no active choices, no agent connected)."""
        self.query_one("#choices").display = False
        self.query_one("#preamble").display = False
        self.query_one("#dwell-bar").display = False
        self.query_one("#speech-log").display = False
        status = self.query_one("#status", Label)
        status_text = "[dim]Ready — demo mode[/dim]" if self._demo else "[dim]Waiting for agent...[/dim]"
        status.update(status_text)
        status.display = True

    # ─── Speech with priority ─────────────────────────────────────

    def session_speak(self, session: Session, text: str, block: bool = True,
                      priority: int = 0, emotion: str = "") -> None:
        """Speak text for a session, respecting priority rules.

        Args:
            emotion: Optional per-call emotion override. Merged with config
                     emotion if both provided. Takes precedence over session override.
        """
        self._touch_session(session)

        # Emit event for remote frontends
        try:
            frontend_api.emit_speech_requested(
                session.session_id, text, blocking=block, priority=priority)
        except Exception:
            pass

        # Log the speech
        entry = SpeechEntry(text=text, priority=priority)
        session.speech_log.append(entry)

        # Update speech log UI if this is the focused session
        if self._is_focused(session.session_id):
            try:
                self.call_from_thread(self._update_speech_log)
            except Exception:
                pass

        if self._is_focused(session.session_id):
            # Foreground: play immediately
            voice_ov = getattr(session, 'voice_override', None)
            # Per-call emotion > session override > config default
            emotion_ov = emotion if emotion else getattr(session, 'emotion_override', None)

            # Urgent messages always interrupt
            if priority >= 1:
                self._tts.stop()

            self._fg_speaking = True
            if block:
                # Use streaming for blocking calls (lower latency)
                self._tts.speak_streaming(text, voice_override=voice_ov,
                                         emotion_override=emotion_ov, block=True)
            else:
                self._tts.speak_async(text, voice_override=voice_ov,
                                     emotion_override=emotion_ov)
            self._fg_speaking = False
        else:
            # Background: queue (urgent goes to front)
            entry.played = False
            if priority >= 1:
                session.unplayed_speech.insert(0, entry)
            else:
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
        # Show pending message count if any
        msgs = getattr(session, 'pending_messages', [])
        msg_info = f" [dim]·[/dim] [{self._cs['purple']}]{len(msgs)} msg{'s' if len(msgs) != 1 else ''}[/{self._cs['purple']}]" if msgs else ""
        status.update(f"[{self._cs['warning']}]⧗[/{self._cs['warning']}] [{session.name}] Waiting for agent...{msg_info} [dim](u=undo)[/dim]")
        status.display = True

    def _request_compact(self) -> None:
        """Request context compaction by returning instructions to the agent."""
        session = self._focused()
        if not session or not session.active:
            self._tts.speak_async("No active session to compact")
            return

        self._tts.play_chime("select")
        self._tts.speak_async("Compacting context")

        compact_instructions = (
            "The user wants you to compact your context window. "
            "To do this, you need to send /compact to your own Claude Code pane via tmux. "
            "Steps:\n"
            "1. Find your own tmux pane: run `tmux-cli list_panes` and find the pane with your Claude Code process\n"
            "2. Send the compact command: `tmux send-keys -t %<your-pane-id> '/compact' Enter`\n"
            "3. Wait a few seconds for compaction to complete\n"
            "4. Then continue with present_choices() as normal\n\n"
            "If tmux-cli is not available, tell the user to type /compact manually."
        )

        session.selection = {"selected": compact_instructions, "summary": "(compact context)"}
        session.selection_event.set()
        self._show_waiting("Compact context")

    def _restart_tui(self) -> None:
        """Restart the TUI backend process.

        Uses os.execv to replace the current process with a fresh one.
        The proxy stays alive so agent MCP connections are preserved.
        Strips --restart from argv since it's a one-time cleanup flag.
        """
        self._tts.speak_async("Restarting TUI in 2 seconds")

        import time as _time

        def _do_restart():
            _time.sleep(2.0)
            # Strip one-time flags that shouldn't persist across restarts
            argv = [a for a in sys.argv if a != "--restart"]
            os.execv(sys.executable, [sys.executable] + argv)

        threading.Thread(target=_do_restart, daemon=True).start()

    def _enter_worktree_mode(self) -> None:
        """Start worktree creation flow.

        Shows options: create worktree (prompts for branch name),
        or fork agent to worktree (spawns new agent in the worktree).
        """
        session = self._focused()
        if not session:
            self._tts.speak_async("No active session")
            return

        self._tts.stop()

        # Enter settings-like modal for worktree options
        self._in_settings = True
        self._setting_edit_mode = False
        self._spawn_options = None
        self._quick_action_options = None
        self._dashboard_mode = False
        self._log_viewer_mode = False
        self._help_mode = False
        self._tab_picker_mode = False

        s = self._cs

        worktree_opts = [
            {"label": "Branch and work here", "summary": "Create worktree, switch this agent to work in it",
             "_action": "branch_here"},
            {"label": "Fork agent to worktree", "summary": "Create worktree and spawn a new agent there (you stay on main)",
             "_action": "fork_agent"},
            {"label": "Cancel", "summary": "Go back",
             "_action": "cancel"},
        ]

        # If agent is already in a worktree, show merge options instead
        cwd = getattr(session, 'cwd', '')
        worktree_dir = os.path.expanduser("~/.config/io-mcp/worktrees")
        if cwd and worktree_dir in cwd:
            worktree_opts = [
                {"label": "Push and create PR", "summary": "Push branch and create a pull request",
                 "_action": "push_pr"},
                {"label": "Merge to main", "summary": "Merge worktree branch into default branch and clean up",
                 "_action": "merge_main"},
                {"label": "Cancel", "summary": "Go back",
                 "_action": "cancel"},
            ]

        self._worktree_options = worktree_opts

        preamble_widget = self.query_one("#preamble", Label)
        preamble_widget.update(f"[bold {s['accent']}]Git Worktree[/bold {s['accent']}]")
        preamble_widget.display = True

        list_view = self.query_one("#choices", ListView)
        list_view.clear()
        for i, opt in enumerate(worktree_opts):
            list_view.append(ChoiceItem(opt["label"], opt.get("summary", ""), index=i + 1, display_index=i))
        list_view.display = True
        list_view.index = 0
        list_view.focus()

        self._tts.speak_async("Git worktree options.")

    def _handle_worktree_select(self, idx: int) -> None:
        """Handle selection in worktree mode."""
        opts = getattr(self, '_worktree_options', [])
        if idx >= len(opts):
            return

        action = opts[idx].get("_action", "cancel")
        self._worktree_options = None
        self._in_settings = False

        session = self._focused()
        if not session:
            return

        if action == "cancel":
            self._exit_settings()
            return

        if action == "branch_here":
            # Ask for branch name via freeform input
            self._worktree_action = "branch_here"
            self._message_mode = False
            session.input_mode = True
            self._freeform_spoken_pos = 0

            self.query_one("#choices").display = False
            inp = self.query_one("#freeform-input", Input)
            inp.placeholder = "Enter branch name (e.g. fix/auth-bug)"
            inp.value = ""
            inp.styles.display = "block"
            inp.focus()

            self._tts.speak_async("Type the branch name")

        elif action == "fork_agent":
            self._worktree_action = "fork_agent"
            session.input_mode = True
            self._freeform_spoken_pos = 0

            self.query_one("#choices").display = False
            inp = self.query_one("#freeform-input", Input)
            inp.placeholder = "Enter branch name for new agent worktree"
            inp.value = ""
            inp.styles.display = "block"
            inp.focus()

            self._tts.speak_async("Type branch name for the new agent's worktree")

        elif action == "push_pr":
            # Queue message to agent to push and create PR
            msgs = getattr(session, 'pending_messages', [])
            if msgs is not None:
                msgs.append(
                    "Push all changes on this branch and create a pull request. "
                    "Include a good title and description. Then present choices."
                )
            self._tts.speak_async("Queued: push and create PR")
            if session.active:
                self._show_choices()
            else:
                self._show_session_waiting(session)

        elif action == "merge_main":
            msgs = getattr(session, 'pending_messages', [])
            if msgs is not None:
                msgs.append(
                    "Merge this branch into the default branch (main/master). "
                    "First ensure all changes are committed and pushed. "
                    "Then merge, clean up the worktree, and switch back to the main branch. "
                    "Present choices when done."
                )
            self._tts.speak_async("Queued: merge to main and clean up worktree")
            if session.active:
                self._show_choices()
            else:
                self._show_session_waiting(session)

    def _create_worktree(self, session: Session, branch_name: str, action: str) -> None:
        """Create a git worktree and either switch to it or spawn an agent there."""
        # Determine repo root from agent's cwd (or current dir)
        cwd = getattr(session, 'cwd', '') or os.getcwd()

        def _do_create():
            try:
                # Get repo name
                result = subprocess.run(
                    ["git", "rev-parse", "--show-toplevel"],
                    cwd=cwd, capture_output=True, text=True, timeout=5,
                )
                if result.returncode != 0:
                    self._tts.speak_async("Not in a git repository")
                    self.call_from_thread(self._restore_choices)
                    return

                repo_root = result.stdout.strip()
                repo_name = os.path.basename(repo_root)

                # Create worktree directory
                worktree_base = os.path.expanduser(f"~/.config/io-mcp/worktrees/{repo_name}")
                worktree_path = os.path.join(worktree_base, branch_name)

                # Create the worktree
                result = subprocess.run(
                    ["git", "worktree", "add", "-b", branch_name, worktree_path],
                    cwd=repo_root, capture_output=True, text=True, timeout=30,
                )
                if result.returncode != 0:
                    err = result.stderr.strip()[:80]
                    self._tts.speak_async(f"Worktree creation failed: {err}")
                    self.call_from_thread(self._restore_choices)
                    return

                if action == "branch_here":
                    # Queue message telling agent to switch to worktree
                    msgs = getattr(session, 'pending_messages', [])
                    msgs.append(
                        f"I've created a git worktree for branch '{branch_name}' at {worktree_path}. "
                        f"Please cd to {worktree_path} and work there from now on. "
                        f"When you're done, I'll help you push, create a PR, and merge back."
                    )
                    session.cwd = worktree_path
                    self._tts.speak_async(f"Worktree created at {branch_name}. Agent will switch there.")
                    if session.active:
                        self.call_from_thread(self._show_choices)
                    else:
                        self.call_from_thread(self._show_session_waiting, session)

                elif action == "fork_agent":
                    # Spawn a new agent in the worktree
                    self._tts.speak_async(f"Worktree created. Spawning agent in {branch_name}.")
                    spawn_opt = {"type": "local", "workdir": worktree_path}
                    self.call_from_thread(self._do_spawn, spawn_opt)

            except Exception as e:
                self._tts.speak_async(f"Error: {str(e)[:80]}")
                self.call_from_thread(self._restore_choices)

        threading.Thread(target=_do_create, daemon=True).start()

    def _enter_multi_select_mode(self) -> None:
        """Enter multi-select mode for the current choices.

        Re-renders the choice list with checkbox indicators. Enter toggles
        each item. Adds "Confirm" and "Confirm with team" at the bottom.
        """
        session = self._focused()
        if not session or not session.active or not session.choices:
            self._tts.speak_async("No choices to multi-select from")
            return

        self._tts.stop()
        self._multi_select_mode = True
        self._multi_select_checked = [False] * len(session.choices)
        self._refresh_multi_select()
        self._tts.speak_async("Multi-select mode. Scroll and press Enter to toggle. Scroll to Confirm when done.")

    def _refresh_multi_select(self) -> None:
        """Redraw the choice list with checkbox state."""
        session = self._focused()
        if not session:
            return

        s = self._cs
        preamble_widget = self.query_one("#preamble", Label)
        checked_count = sum(self._multi_select_checked)
        preamble_widget.update(
            f"[bold {s['purple']}]Multi-select[/bold {s['purple']}] — "
            f"{checked_count} selected "
            f"[dim](enter=toggle, scroll to confirm)[/dim]"
        )
        preamble_widget.display = True

        list_view = self.query_one("#choices", ListView)

        # Remember current position
        current_idx = list_view.index or 0

        list_view.clear()

        # Choices with checkboxes
        for i, c in enumerate(session.choices):
            check = f"[{s['success']}]✓[/{s['success']}]" if self._multi_select_checked[i] else "○"
            label = f" {check}  {c.get('label', '')}"
            summary = c.get('summary', '')
            list_view.append(ChoiceItem(label, summary, index=i + 1, display_index=i))

        # Confirm options
        confirm_idx = len(session.choices)
        list_view.append(ChoiceItem(
            f"[bold {s['success']}]✅ Confirm selection[/bold {s['success']}]",
            f"{checked_count} item(s) — do all selected",
            index=confirm_idx + 1, display_index=confirm_idx,
        ))
        list_view.append(ChoiceItem(
            f"[bold {s['accent']}]🚀 Confirm with team[/bold {s['accent']}]",
            f"{checked_count} item(s) — delegate each to a parallel sub-agent",
            index=confirm_idx + 2, display_index=confirm_idx + 1,
        ))
        list_view.append(ChoiceItem(
            "[dim]Cancel[/dim]", "Exit multi-select, return to choices",
            index=confirm_idx + 3, display_index=confirm_idx + 2,
        ))

        list_view.display = True
        # Restore position
        if current_idx < len(list_view.children):
            list_view.index = current_idx
        else:
            list_view.index = 0
        list_view.focus()

    def _handle_multi_select_enter(self, idx: int) -> None:
        """Handle Enter press in multi-select mode."""
        session = self._focused()
        if not session:
            return

        num_choices = len(session.choices)
        num_checked = len(self._multi_select_checked)

        if idx < num_choices and idx < num_checked:
            # Toggle the choice
            self._multi_select_checked[idx] = not self._multi_select_checked[idx]
            state = "selected" if self._multi_select_checked[idx] else "unselected"
            label = session.choices[idx].get("label", "")
            self._tts.speak_async(f"{label} {state}")
            self._refresh_multi_select()
        elif idx == num_choices:
            # Confirm selection
            self._confirm_multi_select(team=False)
        elif idx == num_choices + 1:
            # Confirm with team
            self._confirm_multi_select(team=True)
        elif idx == num_choices + 2:
            # Cancel
            self._multi_select_mode = False
            self._multi_select_checked = []
            self._show_choices()
            self._tts.speak_async("Multi-select cancelled. Back to choices.")

    def _confirm_multi_select(self, team: bool = False) -> None:
        """Confirm multi-select and return all selected choices as one response."""
        session = self._focused()
        if not session:
            return

        selected = [
            session.choices[i] for i in range(len(session.choices))
            if i < len(self._multi_select_checked) and self._multi_select_checked[i]
        ]

        if not selected:
            self._tts.speak_async("Nothing selected. Toggle some choices first.")
            return

        self._multi_select_mode = False
        self._multi_select_checked = []

        labels = [s.get("label", "") for s in selected]
        combined_label = "; ".join(labels)

        if team:
            response_text = (
                f"The user selected multiple actions to be done IN PARALLEL. "
                f"Start an agent team to handle all of these actions simultaneously. "
                f"Use the same model you are using. "
                f"Actions:\n" + "\n".join(f"- {l}" for l in labels)
            )
            self._tts.speak_async(f"Team mode. {len(selected)} tasks for agent team.")
        else:
            response_text = (
                f"The user selected multiple actions to do sequentially:\n"
                + "\n".join(f"- {l}" for l in labels)
            )
            self._tts.speak_async(f"Confirmed {len(selected)} selections.")

        # Haptic + audio
        self._vibrate(100)
        self._tts.play_chime("select")

        session.selection = {"selected": response_text, "summary": f"(multi-select: {combined_label[:60]})"}
        session.selection_event.set()
        self._show_waiting(f"Multi: {combined_label[:50]}")

    def _enter_tab_picker(self) -> None:
        """Enter tab picking mode.

        Shows all sessions in a list. Scrolling highlights each tab
        (switching to it live). Enter confirms and exits.
        """
        sessions = self.manager.all_sessions()
        if len(sessions) <= 1:
            self._tts.speak_async("Only one tab open. Press t to spawn a new agent.")
            return

        self._tts.stop()

        # Use settings infrastructure for modal
        self._in_settings = True
        self._setting_edit_mode = False
        self._spawn_options = None
        self._quick_action_options = None
        self._dashboard_mode = False
        self._log_viewer_mode = False
        self._help_mode = False
        self._tab_picker_mode = True

        s = self._cs
        preamble_widget = self.query_one("#preamble", Label)
        preamble_widget.update(
            f"[bold {s['accent']}]Switch Tab[/bold {s['accent']}] — "
            f"{len(sessions)} tabs "
            f"[dim](scroll to preview, enter to confirm)[/dim]"
        )
        preamble_widget.display = True

        list_view = self.query_one("#choices", ListView)
        list_view.clear()

        current_idx = 0
        for i, sess in enumerate(sessions):
            indicator = f"[{s['success']}]●[/{s['success']}] " if sess.active else ""
            focused = " ◂" if sess.session_id == self.manager.active_session_id else ""
            label = f"{indicator}{sess.name}{focused}"
            summary = ""
            if sess.speech_log:
                summary = sess.speech_log[-1].text[:60]
            list_view.append(ChoiceItem(label, summary, index=i + 1, display_index=i))
            if sess.session_id == self.manager.active_session_id:
                current_idx = i

        list_view.display = True
        list_view.index = current_idx
        list_view.focus()

        self._tab_picker_sessions = sessions
        self._tts.speak_async(f"Pick a tab. {len(sessions)} tabs. Scrolling switches live.")

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
        """Called when a new session is created (from MCP thread).

        Assigns voice/emotion from rotation lists if configured.
        """
        # Audio cue for new agent connection
        self._tts.play_chime("connect")

        # Assign voice/emotion rotation
        if self._config:
            voice_rot = self._config.tts_voice_rotation
            emotion_rot = self._config.tts_emotion_rotation
            session_idx = self.manager.count() - 1  # 0-based

            if voice_rot:
                session.voice_override = voice_rot[session_idx % len(voice_rot)]
            if emotion_rot:
                session.emotion_override = emotion_rot[session_idx % len(emotion_rot)]

        try:
            self.call_from_thread(self._update_tab_bar)
        except Exception:
            pass

        # Emit for remote frontends
        try:
            frontend_api.emit_session_created(session.session_id, session.name)
        except Exception:
            pass

    def on_session_removed(self, session_id: str) -> None:
        """Called when a session is removed."""
        new_active = self.manager.remove(session_id)

        # Emit for remote frontends
        try:
            frontend_api.emit_session_removed(session_id)
        except Exception:
            pass

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
        """Toggle voice recording mode.
        Works both for choice selection and message queueing (when _message_mode is True).
        """
        session = self._focused()
        if not session:
            return
        # Allow voice input even when session is not active (for message queueing)
        if not session.active and not self._message_mode:
            return
        if session.voice_recording:
            self._stop_voice_recording()
        else:
            # Hide the freeform input if we're in message mode
            if self._message_mode:
                inp = self.query_one("#freeform-input", Input)
                inp.styles.display = "none"
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

        # Audio cue for recording start
        self._tts.play_chime("record_start")

        # Emit recording state for remote frontends
        try:
            frontend_api.emit_recording_state(session.session_id, True)
        except Exception:
            pass

        # Mute TTS — stops current audio and prevents any new playback
        # until unmute() is called in _stop_voice_recording.
        # Graceful fallback if TTSEngine predates mute() (pre-reload).
        if hasattr(self._tts, 'mute'):
            self._tts.mute()
        else:
            self._tts.stop()
            self._tts._muted = True

        # UI update
        self.query_one("#choices").display = False
        self.query_one("#dwell-bar").display = False
        status = self.query_one("#status", Label)
        status.update(f"[bold {self._cs['error']}]● REC[/bold {self._cs['error']}] Recording... [dim](space to stop)[/dim]")
        status.display = True

        # Find binaries
        termux_exec_bin = _find_binary("termux-exec")
        stt_bin = _find_binary("stt")

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

        # Audio cue for recording stop
        self._tts.play_chime("record_stop")
        proc = self._voice_process
        self._voice_process = None

        # Emit recording state for remote frontends
        try:
            frontend_api.emit_recording_state(session.session_id, False)
        except Exception:
            pass

        status = self.query_one("#status", Label)
        status.update(f"[{self._cs['blue']}]⧗[/{self._cs['blue']}] Transcribing...")

        def _process():
            termux_exec_bin = _find_binary("termux-exec")
            stt_bin = _find_binary("stt")
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

            # Unmute TTS now that recording is stopped
            if hasattr(self._tts, 'unmute'):
                self._tts.unmute()
            else:
                self._tts._muted = False

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
                # Convert recorded audio to WAV for direct API upload
                ffmpeg_bin = _find_binary("ffmpeg")
                if not ffmpeg_bin:
                    self._tts.speak_async("ffmpeg not found")
                    self.call_from_thread(self._restore_choices)
                    return

                # Convert to WAV (for direct API upload, not piped through VAD)
                import tempfile
                wav_file = os.path.join(tempfile.gettempdir(), "io-mcp-stt.wav")
                ffmpeg_result = subprocess.run(
                    [ffmpeg_bin, "-y", "-i", rec_file,
                     "-ar", "24000", "-ac", "1", wav_file],
                    capture_output=True, timeout=30,
                )
                if ffmpeg_result.returncode != 0:
                    self._tts.speak_async("Audio conversion failed")
                    self.call_from_thread(self._restore_choices)
                    return

                # Try direct API transcription first (faster, no VAD chunking)
                transcript = ""
                stderr_text = ""
                if self._config and self._config.stt_api_key:
                    transcript = self._transcribe_via_api(wav_file)

                # Fallback to stt CLI if API call failed
                if not transcript:
                    ffmpeg_proc = subprocess.Popen(
                        [ffmpeg_bin, "-y", "-i", rec_file,
                         "-f", "s16le", "-ar", "24000", "-ac", "1", "-"],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.DEVNULL,
                    )

                    # Build stt command from config (explicit flags)
                    if self._config:
                        stt_args = [stt_bin] + self._config.stt_cli_args()
                    else:
                        stt_args = [stt_bin, "--stdin"]

                    stt_proc = subprocess.Popen(
                        stt_args,
                        stdin=ffmpeg_proc.stdout,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        env=env,
                    )
                    ffmpeg_proc.stdout.close()
                    stdout, stderr = stt_proc.communicate(timeout=120)
                    transcript = stdout.decode("utf-8", errors="replace").strip()
                    stderr_text = stderr.decode("utf-8", errors="replace").strip() if stderr else ""

                # Clean up WAV
                try:
                    os.unlink(wav_file)
                except Exception:
                    pass
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

                # If in message queue mode, queue instead of selecting
                if self._message_mode:
                    self._message_mode = False
                    msgs = getattr(session, 'pending_messages', None)
                    if msgs is not None:
                        msgs.append(transcript)
                    count = len(msgs) if msgs else 1
                    self._tts.speak_async(f"Message queued: {transcript[:50]}. {count} pending.")
                    if session.active:
                        self.call_from_thread(self._restore_choices)
                    else:
                        self.call_from_thread(self._show_session_waiting, session)
                else:
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

    def _show_notifications(self) -> None:
        """Fetch and display Android notifications via termux-notification-list.

        Shows notifications in the UI and reads a summary via TTS.
        """
        import json as json_mod

        termux_exec_bin = _find_binary("termux-exec")
        if not termux_exec_bin:
            self._tts.speak_async("termux-exec not found. Can't check notifications.")
            return

        self._tts.speak_async("Checking notifications")

        # Show loading state
        status = self.query_one("#status", Label)
        status.update(f"[{self._cs['blue']}]⧗[/{self._cs['blue']}] Checking notifications...")
        status.display = True
        self.query_one("#choices").display = False

        def _fetch():
            try:
                result = subprocess.run(
                    [termux_exec_bin, "termux-notification-list"],
                    capture_output=True, text=True, timeout=10,
                )
                if result.returncode != 0:
                    self._tts.speak_async("Failed to get notifications")
                    self.call_from_thread(self._restore_choices)
                    return

                notifications = json_mod.loads(result.stdout)
                if not notifications:
                    self._tts.speak_async("No notifications")
                    self.call_from_thread(self._restore_choices)
                    return

                # Filter to interesting notifications (skip system/ongoing)
                interesting = []
                for n in notifications:
                    title = n.get("title", "")
                    content = n.get("content", "")
                    pkg = n.get("packageName", "")
                    # Skip io-mcp's own and common system notifications
                    if any(skip in pkg for skip in ["termux", "android.system", "inputmethod"]):
                        continue
                    if title or content:
                        # Shorten package name for readability
                        app_name = pkg.split(".")[-1] if pkg else "unknown"
                        interesting.append({
                            "app": app_name,
                            "title": title,
                            "content": content,
                        })

                if not interesting:
                    self._tts.speak_async("No new notifications")
                    self.call_from_thread(self._restore_choices)
                    return

                # Read out notifications — batch into one TTS call for speed
                count = len(interesting)
                parts = [f"{count} notification{'s' if count != 1 else ''}."]

                for n in interesting[:5]:  # limit to 5
                    title = n['title'][:60] if n['title'] else ""
                    content = n['content'][:40] if n['content'] and n['content'] != n['title'] else ""
                    text = f"{n['app']}: {title}"
                    if content:
                        text += f". {content}"
                    parts.append(text)

                self._tts.speak(" ".join(parts))
                self.call_from_thread(self._restore_choices)

            except Exception as e:
                self._tts.speak_async(f"Notification check failed: {str(e)[:60]}")
                self.call_from_thread(self._restore_choices)

        threading.Thread(target=_fetch, daemon=True).start()

    def _transcribe_via_api(self, wav_path: str) -> str:
        """Send a WAV file directly to the transcription API.

        Bypasses the stt tool's VAD pipeline — sends the entire recording
        as a single API request for faster, more reliable transcription
        of pre-recorded audio.

        Returns the transcript text, or empty string on failure.
        """
        import urllib.request
        import uuid

        if not self._config:
            return ""

        model = self._config.stt_model_name
        api_key = self._config.stt_api_key
        base_url = self._config.stt_base_url

        if not api_key:
            return ""

        # mai-ears-1 uses a different API endpoint (chat completions)
        # Fall back to stt CLI for that model
        if model == "mai-ears-1":
            return ""

        try:
            with open(wav_path, "rb") as f:
                wav_data = f.read()

            # Build multipart/form-data
            boundary = uuid.uuid4().hex
            body = (
                (
                    f"--{boundary}\r\n"
                    f'Content-Disposition: form-data; name="file"; filename="audio.wav"\r\n'
                    f"Content-Type: audio/wav\r\n\r\n"
                ).encode()
                + wav_data
                + (
                    f"\r\n--{boundary}\r\n"
                    f'Content-Disposition: form-data; name="model"\r\n\r\n'
                    f"{model}\r\n"
                    f"--{boundary}\r\n"
                    f'Content-Disposition: form-data; name="response_format"\r\n\r\n'
                    f"json\r\n"
                    f"--{boundary}--\r\n"
                ).encode()
            )

            url = f"{base_url.rstrip('/')}/v1/audio/transcriptions"
            req = urllib.request.Request(
                url,
                data=body,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": f"multipart/form-data; boundary={boundary}",
                },
                method="POST",
            )

            import json as json_mod
            with urllib.request.urlopen(req, timeout=60) as resp:
                result = json_mod.loads(resp.read())
                return result.get("text", "").strip()

        except Exception:
            return ""

    # ─── Settings menu ────────────────────────────────────────────

    def action_toggle_settings(self) -> None:
        """Toggle settings menu. Always available regardless of agent connection."""
        if self._in_settings:
            self._exit_settings()
            return
        self._enter_settings()

    def _enter_settings(self) -> None:
        """Show settings menu."""
        session = self._focused()
        if session:
            session.in_settings = True
            session.reading_options = False
        self._in_settings = True
        self._setting_edit_mode = False

        scheme = getattr(self, '_color_scheme', DEFAULT_SCHEME)
        self._settings_items = [
            {"label": "Speed", "key": "speed",
             "summary": f"Current: {self.settings.speed:.1f}"},
            {"label": "Voice", "key": "voice",
             "summary": f"Current: {self.settings.voice}"},
            {"label": "Emotion", "key": "emotion",
             "summary": f"Current: {self.settings.emotion}"},
            {"label": "TTS model", "key": "tts_model",
             "summary": f"Current: {self.settings.tts_model}"},
            {"label": "STT model", "key": "stt_model",
             "summary": f"Current: {self.settings.stt_model}"},
            {"label": "Color scheme", "key": "color_scheme",
             "summary": f"Current: {scheme}"},
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
        self._in_settings = False
        self._setting_edit_mode = False
        self._spawn_options = None
        self._quick_action_options = None
        self._dashboard_mode = False
        self._log_viewer_mode = False
        self._help_mode = False
        self._tab_picker_mode = False

        # Guard: prevent the Enter keypress that triggered "close" from
        # also firing _do_select on the freshly-restored choice list.
        self._settings_just_closed = True
        self.set_timer(0.1, self._clear_settings_guard)

        # UI first, then TTS
        if session and session.active:
            self._show_choices()
            self._tts.stop()
            self._tts.speak_async("Back to choices")
        else:
            self._show_idle()
            self._tts.stop()
            self._tts.speak_async("Settings closed")

    def _clear_settings_guard(self) -> None:
        """Clear the settings-just-closed guard after a frame."""
        self._settings_just_closed = False

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

        elif key == "tts_model":
            self._setting_edit_values = self.settings.get_tts_models()
            current = self.settings.tts_model
            self._setting_edit_index = (
                self._setting_edit_values.index(current)
                if current in self._setting_edit_values else 0
            )

        elif key == "emotion":
            self._setting_edit_values = self.settings.get_emotions()
            current = self.settings.emotion
            self._setting_edit_index = (
                self._setting_edit_values.index(current)
                if current in self._setting_edit_values else 0
            )

        elif key == "stt_model":
            self._setting_edit_values = self.settings.get_stt_models()
            current = self.settings.stt_model
            self._setting_edit_index = (
                self._setting_edit_values.index(current)
                if current in self._setting_edit_values else 0
            )

        elif key == "color_scheme":
            self._setting_edit_values = list(COLOR_SCHEMES.keys())
            current = getattr(self, '_color_scheme', DEFAULT_SCHEME)
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
        elif key == "tts_model":
            self.settings.tts_model = value
            # Voice list may have changed — voice is reset to new model default
        elif key == "emotion":
            self.settings.emotion = value
        elif key == "stt_model":
            self.settings.stt_model = value
        elif key == "color_scheme":
            self._color_scheme = value
            self._cs = get_scheme(value)
            self.__class__.CSS = _build_css(value)
            # Save to config
            if self._config:
                self._config.raw.setdefault("config", {})["colorScheme"] = value
                self._config.save()
            self.title = "io-mcp"

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

        # Haptic feedback on scroll (short buzz)
        self._vibrate(30)

        session = self._focused()

        # In setting edit mode, read the value
        if self._setting_edit_mode:
            if isinstance(event.item, ChoiceItem):
                val = self._setting_edit_values[event.item.display_index] if event.item.display_index < len(self._setting_edit_values) else ""
                self._tts.speak_async(val)
            return

        # In settings mode
        if self._in_settings:
            # Log viewer: read the full speech entry text
            if getattr(self, '_log_viewer_mode', False):
                if isinstance(event.item, ChoiceItem) and session:
                    idx = event.item.display_index
                    speech_log = getattr(session, 'speech_log', [])
                    if idx < len(speech_log):
                        self._tts.speak_async(speech_log[idx].text)
                return
            # Help screen: read the shortcut description
            if getattr(self, '_help_mode', False):
                if isinstance(event.item, ChoiceItem):
                    idx = event.item.display_index
                    shortcuts = getattr(self, '_help_shortcuts', [])
                    if idx < len(shortcuts):
                        key, desc = shortcuts[idx]
                        self._tts.speak_async(f"{key}. {desc}")
                return
            # Tab picker: switch to the highlighted tab live
            if getattr(self, '_tab_picker_mode', False):
                if isinstance(event.item, ChoiceItem):
                    idx = event.item.display_index
                    sessions = getattr(self, '_tab_picker_sessions', [])
                    if idx < len(sessions):
                        self._tts.speak_async(sessions[idx].name)
                return
            if isinstance(event.item, ChoiceItem):
                s = self._settings_items[event.item.display_index] if event.item.display_index < len(self._settings_items) else None
                if s:
                    text = f"{s['label']}. {s.get('summary', '')}" if s.get('summary') else s['label']
                    self._tts.speak_async(text)
            return

        if not session or not session.active:
            return

        # Multi-select mode: speak checkbox items and action buttons
        if self._multi_select_mode and isinstance(event.item, ChoiceItem):
            idx = event.item.display_index
            num_choices = len(session.choices)
            if idx < num_choices:
                check = "checked" if (idx < len(self._multi_select_checked) and self._multi_select_checked[idx]) else "unchecked"
                label = session.choices[idx].get("label", "")
                self._tts.speak_async(f"{label}, {check}")
            elif idx == num_choices:
                checked_count = sum(self._multi_select_checked) if self._multi_select_checked else 0
                self._tts.speak_async(f"Confirm selection. {checked_count} items selected.")
            elif idx == num_choices + 1:
                checked_count = sum(self._multi_select_checked) if self._multi_select_checked else 0
                self._tts.speak_async(f"Confirm with team. {checked_count} items for parallel agents.")
            elif idx == num_choices + 2:
                self._tts.speak_async("Cancel multi-select.")
            return

        # During intro/options readout, suppress highlight-triggered speech.
        # User scrolling is handled by on_mouse_scroll which sets
        # intro_speaking/reading_options = False before the highlight changes.
        if getattr(session, 'intro_speaking', False) or getattr(session, 'reading_options', False):
            return

        if isinstance(event.item, ChoiceItem):
            logical = event.item.choice_index
            if logical > 0:
                ci = logical - 1
                if ci >= len(session.choices):
                    return
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
                # Deduplicate — don't repeat the same text twice in a row
                if text != self._last_spoken_text:
                    self._last_spoken_text = text
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
        # Multi-select mode: toggle or confirm
        if self._multi_select_mode and isinstance(event.item, ChoiceItem):
            self._handle_multi_select_enter(event.item.display_index)
            return
        if self._in_settings:
            if isinstance(event.item, ChoiceItem):
                idx = event.item.display_index
                # Check if we're in spawn menu
                spawn_opts = getattr(self, '_spawn_options', None)
                if spawn_opts and idx < len(spawn_opts):
                    self._in_settings = False
                    self._do_spawn(spawn_opts[idx])
                    self._spawn_options = None
                    return
                # Check if we're in log viewer mode (Enter closes it)
                if getattr(self, '_log_viewer_mode', False):
                    self._log_viewer_mode = False
                    self._exit_settings()
                    return
                # Check if we're in help mode (Enter closes it)
                if getattr(self, '_help_mode', False):
                    self._help_mode = False
                    self._exit_settings()
                    return
                # Check if we're in tab picker mode
                if getattr(self, '_tab_picker_mode', False):
                    self._tab_picker_mode = False
                    self._in_settings = False
                    sessions = getattr(self, '_tab_picker_sessions', [])
                    if idx < len(sessions):
                        self._switch_to_session(sessions[idx])
                    return
                # Check if we're in worktree mode
                if getattr(self, '_worktree_options', None):
                    self._handle_worktree_select(idx)
                    return
                # Check if we're in dashboard mode
                if getattr(self, '_dashboard_mode', False):
                    self._dashboard_mode = False
                    self._in_settings = False
                    sessions = self.manager.all_sessions()
                    if idx < len(sessions):
                        self._switch_to_session(sessions[idx])
                    return
                # Check if we're in quick action menu
                qa_opts = getattr(self, '_quick_action_options', None)
                if qa_opts and idx < len(qa_opts):
                    self._in_settings = False
                    action = qa_opts[idx].get("_action")
                    self._quick_action_options = None
                    if action is None:
                        self._exit_settings()
                    else:
                        self._execute_quick_action(action)
                    return
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

    def _interrupt_readout(self) -> None:
        """Interrupt intro/options readout when user scrolls."""
        session = self._focused()
        if session:
            if getattr(session, 'intro_speaking', False):
                session.intro_speaking = False
                self._tts.stop()
            if getattr(session, 'reading_options', False):
                session.reading_options = False
                self._tts.stop()

    def action_cursor_down(self) -> None:
        session = self._focused()
        if session and (session.input_mode or session.voice_recording):
            return
        self._interrupt_readout()
        list_view = self.query_one("#choices", ListView)
        if list_view.display:
            list_view.action_cursor_down()

    def action_cursor_up(self) -> None:
        session = self._focused()
        if session and (session.input_mode or session.voice_recording):
            return
        self._interrupt_readout()
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
        if (self._in_settings or self._setting_edit_mode or (session and session.active)) and self._scroll_allowed():
            self._interrupt_readout()
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
        if (self._in_settings or self._setting_edit_mode or (session and session.active)) and self._scroll_allowed():
            self._interrupt_readout()
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
        # Enter stops voice recording (same as space)
        if session and session.voice_recording:
            self._stop_voice_recording()
            return
        # Multi-select mode: toggle or confirm
        if self._multi_select_mode:
            list_view = self.query_one("#choices", ListView)
            idx = list_view.index or 0
            self._handle_multi_select_enter(idx)
            return
        if self._in_settings:
            list_view = self.query_one("#choices", ListView)
            idx = list_view.index or 0
            # Check if we're in spawn menu
            spawn_opts = getattr(self, '_spawn_options', None)
            if spawn_opts and idx < len(spawn_opts):
                self._in_settings = False
                self._do_spawn(spawn_opts[idx])
                self._spawn_options = None
                return
            # Check if we're in dashboard mode
            if getattr(self, '_dashboard_mode', False):
                self._dashboard_mode = False
                self._in_settings = False
                sessions = self.manager.all_sessions()
                if idx < len(sessions):
                    self._switch_to_session(sessions[idx])
                return
            # Check if we're in quick action menu
            qa_opts = getattr(self, '_quick_action_options', None)
            if qa_opts and idx < len(qa_opts):
                self._in_settings = False
                action = qa_opts[idx].get("_action")
                self._quick_action_options = None
                if action is None:
                    self._exit_settings()
                else:
                    self._execute_quick_action(action)
                return
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

    def action_queue_message(self) -> None:
        """Open text input to queue a message for the agent's next response.
        Also supports voice input — press space to record a voice message.
        """
        session = self._focused()
        if not session:
            return
        # Allow queueing even when session is not active (agent is working)
        if getattr(session, 'input_mode', False) or getattr(session, 'voice_recording', False):
            return
        self._message_mode = True
        self._freeform_spoken_pos = 0

        # UI
        self.query_one("#choices").display = False
        self.query_one("#dwell-bar").display = False
        inp = self.query_one("#freeform-input", Input)
        inp.placeholder = "Type message (Enter to send) or press Space to record voice message"
        inp.value = ""
        inp.styles.display = "block"
        inp.focus()

        self._tts.stop()
        self._tts.speak_async("Type or speak a message for the agent")

    def action_filter_choices(self) -> None:
        """Open filter input to narrow the choice list by typing."""
        session = self._focused()
        if not session or not session.active:
            return
        if session.input_mode or session.voice_recording or self._in_settings:
            return
        if self._filter_mode:
            return

        self._filter_mode = True
        session.reading_options = False
        self._tts.stop()

        filter_inp = self.query_one("#filter-input", Input)
        filter_inp.value = ""
        filter_inp.styles.display = "block"
        filter_inp.focus()

        self._tts.speak_async("Type to filter choices")

    def _apply_filter(self, query: str) -> None:
        """Filter the choices ListView to show only matching items."""
        session = self._focused()
        if not session or not session.active:
            return

        list_view = self.query_one("#choices", ListView)
        list_view.clear()
        q = query.lower()

        # Always show extras (but filtered too if query is set)
        for i, e in enumerate(EXTRA_OPTIONS):
            logical_idx = -(len(EXTRA_OPTIONS) - 1 - i)
            if q and q not in e["label"].lower() and q not in e.get("summary", "").lower():
                continue
            list_view.append(ChoiceItem(
                e["label"], e.get("summary", ""),
                index=logical_idx, display_index=i,
            ))

        # Filter real choices
        match_count = 0
        for i, c in enumerate(session.choices):
            label = c.get("label", "")
            summary = c.get("summary", "")
            if q and q not in label.lower() and q not in summary.lower():
                continue
            list_view.append(ChoiceItem(
                label, summary,
                index=i + 1, display_index=len(EXTRA_OPTIONS) + i,
            ))
            match_count += 1

        # Focus the first real match if any
        if match_count > 0 and len(list_view.children) > 0:
            # Find first real choice in filtered list
            for j, child in enumerate(list_view.children):
                if isinstance(child, ChoiceItem) and child.choice_index > 0:
                    list_view.index = j
                    break

    def _exit_filter(self) -> None:
        """Exit filter mode and restore the full choice list."""
        self._filter_mode = False
        filter_inp = self.query_one("#filter-input", Input)
        filter_inp.styles.display = "none"

        # Restore full choices list
        session = self._focused()
        if session and session.active:
            self._show_choices()
            list_view = self.query_one("#choices", ListView)
            list_view.focus()

    @on(Input.Changed, "#filter-input")
    def on_filter_changed(self, event: Input.Changed) -> None:
        """Filter choices as user types."""
        if not self._filter_mode:
            return
        self._apply_filter(event.value)

    @on(Input.Submitted, "#filter-input")
    def on_filter_submitted(self, event: Input.Submitted) -> None:
        """Submit filter — exit filter mode, keep filtered view and focus list."""
        if not self._filter_mode:
            return
        self._filter_mode = False
        filter_inp = self.query_one("#filter-input", Input)
        filter_inp.styles.display = "none"

        # Keep the filtered list, just move focus to it
        list_view = self.query_one("#choices", ListView)
        list_view.focus()

        count = sum(1 for c in list_view.children if isinstance(c, ChoiceItem) and c.choice_index > 0)
        self._tts.speak_async(f"{count} matches")

    @on(Input.Changed, "#freeform-input")
    def on_freeform_changed(self, event: Input.Changed) -> None:
        session = self._focused()
        # Readback works for both freeform input and message queue mode
        if not session or (not session.input_mode and not self._message_mode):
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

        self._vibrate(100)  # Haptic feedback on submit

        # Worktree branch name input
        worktree_action = getattr(self, '_worktree_action', None)
        if worktree_action:
            self._worktree_action = None
            session.input_mode = False
            event.input.styles.display = "none"
            event.input.placeholder = "Type your reply, press Enter to send, Escape to cancel"
            self._freeform_tts.stop()
            self._create_worktree(session, text, worktree_action)
            return

        # Message queue mode — queue the message, don't select
        if self._message_mode:
            self._message_mode = False
            event.input.styles.display = "none"
            event.input.placeholder = "Type your reply, press Enter to send, Escape to cancel"
            msgs = getattr(session, 'pending_messages', None)
            if msgs is not None:
                msgs.append(text)
            self._freeform_tts.stop()
            self._tts.stop()
            count = len(msgs) if msgs else 1
            self._tts.speak_async(f"Message queued. {count} pending.")
            if session.active:
                self._restore_choices()
            else:
                self._show_session_waiting(session)
            return

        # Normal freeform input — select with the text
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
        self._message_mode = False
        self._freeform_tts.stop()
        inp = self.query_one("#freeform-input", Input)
        inp.styles.display = "none"
        inp.placeholder = "Type your reply, press Enter to send, Escape to cancel"
        self._restore_choices()
        self._tts.speak_async("Cancelled.")

    def on_key(self, event) -> None:
        """Handle Escape in freeform/voice/settings/filter mode.
        Also intercepts space in message mode to trigger voice recording.
        """
        session = self._focused()
        # In message mode, space triggers voice recording instead of typing
        if self._message_mode and event.key == "space" and not (session and session.voice_recording):
            event.prevent_default()
            event.stop()
            self.action_voice_input()
            return
        if self._filter_mode and event.key == "escape":
            self._exit_filter()
            self._tts.speak_async("Filter cleared")
            event.prevent_default()
            event.stop()
        elif self._message_mode and event.key == "escape":
            self._cancel_freeform()
            event.prevent_default()
            event.stop()
        elif session and session.input_mode and event.key == "escape":
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
            if hasattr(self._tts, 'unmute'):
                self._tts.unmute()
            else:
                self._tts._muted = False
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
        elif self._in_settings and event.key == "escape":
            self._exit_settings()
            event.prevent_default()
            event.stop()

    def _pick_by_number(self, n: int) -> None:
        """Immediately select option by 1-based number."""
        session = self._focused()
        if not session or not session.active or session.input_mode or session.voice_recording or self._in_settings:
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

    def action_hot_reload(self) -> None:
        """Hot-reload the tui module, monkey-patching methods onto this app.

        Reimports io_mcp.tui and copies all methods from the fresh IoMcpApp
        class onto this instance's class. Sessions, MCP server, and
        connections stay alive — only method implementations change.
        Also reloads EXTRA_OPTIONS.
        """
        import importlib
        import inspect
        self._tts.stop()

        try:
            # Reload the modules
            import io_mcp.tts as tts_mod
            import io_mcp.tui as tui_mod
            importlib.reload(tts_mod)
            importlib.reload(tui_mod)

            # Update EXTRA_OPTIONS global
            global EXTRA_OPTIONS
            EXTRA_OPTIONS = tui_mod.EXTRA_OPTIONS

            # Monkey-patch only methods defined directly on IoMcpApp
            # (not inherited from Textual App/Widget/etc.)
            fresh_cls = tui_mod.IoMcpApp
            for name, method in inspect.getmembers(fresh_cls, predicate=inspect.isfunction):
                # Only patch methods defined in our module
                if method.__module__ and "io_mcp" in method.__module__:
                    try:
                        setattr(self.__class__, name, method)
                    except (AttributeError, TypeError):
                        pass

            # Also update class-level constants we control
            for attr in ("CSS", "BINDINGS"):
                if hasattr(fresh_cls, attr):
                    try:
                        setattr(self.__class__, attr, getattr(fresh_cls, attr))
                    except (AttributeError, TypeError):
                        pass

            # Ensure TTS is unmuted after reload
            self._tts._muted = False

            # Reload config from disk
            if self._config:
                self._config.reload()

            self._tts.speak_async("Reloaded")
        except Exception as e:
            self._tts.speak_async(f"Reload failed: {str(e)[:80]}")

    def _do_select(self) -> None:
        """Finalize the current selection."""
        if getattr(self, '_settings_just_closed', False):
            return
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

        # Haptic feedback on selection (longer buzz)
        self._vibrate(100)

        # Audio cue
        self._tts.play_chime("select")

        self._tts.stop()
        self._tts.speak_async(f"Selected: {label}")

        # Record in history
        try:
            history = getattr(session, 'history', None)
            if history is not None:
                history.append(HistoryEntry(
                    label=label, summary=summary, preamble=session.preamble))
        except Exception:
            pass

        session.selection = {"selected": label, "summary": summary}
        session.selection_event.set()

        # Emit event for remote frontends
        try:
            frontend_api.emit_selection_made(session.session_id, label, summary)
        except Exception:
            pass

        self._show_waiting(label)

    def _handle_extra_select(self, logical_index: int) -> None:
        """Handle selection of extra options.

        Display order (top to bottom): -3=Record, -2=Fast, -1=Voice, 0=Settings.
        Maps logical_index to EXTRA_OPTIONS array via: ei = len(EXTRA_OPTIONS) - 1 + logical_index.
        """
        self._tts.stop()
        self._vibrate(100)  # Haptic feedback on extra selection

        ei = len(EXTRA_OPTIONS) - 1 + logical_index
        if ei < 0 or ei >= len(EXTRA_OPTIONS):
            return

        label = EXTRA_OPTIONS[ei]["label"]
        if label == "Record response":
            self.action_voice_input()
        elif label == "Multi select":
            self._enter_multi_select_mode()
        elif label == "Branch to worktree":
            self._enter_worktree_mode()
        elif label == "Compact context":
            self._request_compact()
        elif label == "Switch tab":
            self._enter_tab_picker()
        elif label == "Fast toggle":
            msg = self.settings.toggle_fast()
            self._tts.clear_cache()
            self._tts.speak_async(msg)
        elif label == "Voice toggle":
            msg = self.settings.toggle_voice()
            self._tts.clear_cache()
            self._tts.speak_async(msg)
        elif label == "New agent":
            self.action_spawn_agent()
        elif label == "Dashboard":
            self.action_dashboard()
        elif label == "Settings":
            self._enter_settings()
        elif label == "Restart TUI":
            self._restart_tui()
        elif label == "Notifications":
            self._show_notifications()
        elif label == "History":
            self._show_history()
        elif label == "Queue message":
            self.action_queue_message()

    def _show_history(self) -> None:
        """Read out recent selection history for the focused session."""
        session = self._focused()
        if not session:
            self._tts.speak_async("No session active")
            return

        history = getattr(session, 'history', [])
        if not history:
            self._tts.speak_async("No history yet for this session")
            return

        # Read the last 5 selections
        recent = history[-5:]
        count = len(history)
        self._tts.speak_async(f"{count} total selections. Last {len(recent)}:")
        for i, entry in enumerate(reversed(recent), 1):
            self._tts.speak(f"{i}. {entry.label}")

    def action_undo_selection(self) -> None:
        """Undo the last selection — signal the server to re-present choices.

        Only works when the session is in 'waiting for agent' state
        (selection was made, agent hasn't responded yet). Sets a special
        sentinel that the server's present_choices loop recognizes.
        """
        session = self._focused()
        if not session:
            return
        if session.input_mode or session.voice_recording or self._in_settings:
            return

        # Undo only works right after a selection, before agent responds
        # Check if we're in waiting state (selection was set, but event was already signaled)
        # We can also undo during active choices (wrong scroll position)
        if session.active:
            # During active choices: just speak current position reminder
            self._tts.stop()
            self._tts.speak_async("Already in choices. Scroll to pick.")
            return

        # After selection: check if we have choices to go back to
        last_choices = getattr(session, 'last_choices', [])
        last_preamble = getattr(session, 'last_preamble', '')
        if not last_choices:
            self._tts.speak_async("Nothing to undo")
            return

        # Set the undo sentinel — the server loop will re-present
        self._vibrate(100)
        self._tts.stop()
        self._tts.speak_async("Undoing selection")

        # Re-activate the session with the saved choices
        session.selection = {"selected": "_undo", "summary": ""}
        session.selection_event.set()

    @_safe_action
    def action_spawn_agent(self) -> None:
        """Spawn a new Claude Code agent instance.

        Shows a menu of spawn targets:
        - Local (current machine, in tmux)
        - Remote hosts from config

        The spawned agent auto-connects to io-mcp via the MCP plugin.
        """
        session = self._focused()
        if session and (session.input_mode or session.voice_recording):
            return
        if self._in_settings or self._filter_mode:
            return

        self._tts.stop()

        # Build spawn options
        options = [{"label": "Local agent", "summary": "Spawn Claude Code on this machine in a new tmux window"}]

        # Add remote hosts from config
        if self._config:
            for host in self._config.agent_hosts:
                # Support both string ("hostname") and dict ({name, host, workdir}) formats
                if isinstance(host, str):
                    name = host
                    hostname = host
                    workdir = "~"
                else:
                    name = host.get("name", host.get("host", "?"))
                    hostname = host.get("host", "")
                    workdir = host.get("workdir", "~")
                options.append({
                    "label": f"Remote: {name}",
                    "summary": f"SSH to {hostname}, work in {workdir}",
                    "_host": hostname,
                    "_workdir": workdir,
                })

        options.append({"label": "Cancel", "summary": "Go back"})

        # Show spawn menu using settings-style UI
        self._in_settings = True
        self._setting_edit_mode = False

        # Store spawn options for selection handler
        self._spawn_options = options

        preamble_widget = self.query_one("#preamble", Label)
        preamble_widget.update(f"[bold {self._cs['accent']}]Spawn New Agent[/bold {self._cs['accent']}]")
        preamble_widget.display = True

        list_view = self.query_one("#choices", ListView)
        list_view.clear()
        for i, opt in enumerate(options):
            list_view.append(ChoiceItem(
                opt["label"], opt.get("summary", ""),
                index=i + 1, display_index=i,
            ))
        list_view.display = True
        list_view.index = 0
        list_view.focus()

        self._tts.speak_async("Spawn a new agent. Pick a target.")

    def _do_spawn(self, option: dict) -> None:
        """Execute the agent spawn in a background thread."""
        label = option.get("label", "")
        host = option.get("_host", "")
        workdir = option.get("_workdir", "")

        if label == "Cancel":
            self._exit_settings()
            return

        self._tts.speak_async(f"Spawning {label}")

        def _spawn():
            try:
                import shutil
                tmux = shutil.which("tmux")
                if not tmux:
                    self._tts.speak_async("tmux not found. Cannot spawn agent.")
                    self.call_from_thread(self._exit_settings)
                    return

                claude_bin = shutil.which("claude")
                if not claude_bin and not host:
                    self._tts.speak_async("claude not found. Cannot spawn agent.")
                    self.call_from_thread(self._exit_settings)
                    return

                # Generate a session name
                import time as _time
                ts = int(_time.time()) % 10000
                session_name = f"io-agent-{ts}"

                io_mcp_url = os.environ.get("IO_MCP_URL", "")

                if host:
                    # Remote spawn via SSH + tmux
                    workdir_resolved = workdir or "~"
                    remote_cmd = (
                        f"cd {workdir_resolved} && "
                        f"IO_MCP_URL={io_mcp_url} "
                        f"claude -a io-mcp --dangerously-skip-permissions"
                    )
                    cmd = [
                        tmux, "new-window", "-n", session_name,
                        f"ssh -t {host} '{remote_cmd}'"
                    ]
                else:
                    # Local spawn in tmux
                    workdir_resolved = workdir or (
                        self._config.agent_default_workdir if self._config else "~"
                    )
                    workdir_expanded = os.path.expanduser(workdir_resolved)
                    cmd = [
                        tmux, "new-window", "-n", session_name,
                        f"cd {workdir_expanded} && IO_MCP_URL={io_mcp_url} claude -a io-mcp --dangerously-skip-permissions"
                    ]

                result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
                if result.returncode == 0:
                    self._tts.speak_async(f"Agent spawned: {session_name}. It will connect shortly.")
                else:
                    err = result.stderr[:100] if result.stderr else "unknown error"
                    self._tts.speak_async(f"Spawn failed: {err}")

            except Exception as e:
                self._tts.speak_async(f"Spawn error: {str(e)[:80]}")

            self.call_from_thread(self._exit_settings)

        threading.Thread(target=_spawn, daemon=True).start()

    @_safe_action
    def action_toggle_conversation(self) -> None:
        """Toggle conversation mode on/off.

        In conversation mode, the agent speaks and then the TUI
        auto-starts voice recording for your reply. No choice menus.
        Press again to exit and return to normal choice-based interaction.
        """
        session = self._focused()
        if session and (session.input_mode or session.voice_recording):
            return
        if self._in_settings or self._filter_mode:
            return

        self._conversation_mode = not self._conversation_mode
        self._tts.stop()

        if self._conversation_mode:
            self._tts.play_chime("convo_on")
            self._tts.speak_async("Conversation mode on. I'll listen after each response.")
        else:
            self._tts.play_chime("convo_off")
            self._tts.speak_async("Conversation mode off. Back to choices.")
            # If session is active, restore the choices UI
            if session and session.active:
                self.call_from_thread(self._show_choices)

    @_safe_action
    def action_dashboard(self) -> None:
        """Show a dashboard overview of all agent sessions.

        Displays each agent's name, status, last speech, and elapsed
        time since last activity. Narrates a summary via TTS.
        Press d or Escape to return.
        """
        session = self._focused()
        if session and (session.input_mode or session.voice_recording):
            return
        if self._in_settings or self._filter_mode:
            return

        self._tts.stop()
        sessions = self.manager.all_sessions()

        if not sessions:
            self._tts.speak_async("No active agents")
            return

        # Build dashboard display
        self._in_settings = True
        self._setting_edit_mode = False
        self._spawn_options = None
        self._quick_action_options = None
        self._dashboard_mode = False
        self._dashboard_mode = True

        s = getattr(self, '_cs', get_scheme(DEFAULT_SCHEME))

        preamble_widget = self.query_one("#preamble", Label)
        preamble_widget.update(f"[bold {s['accent']}]Dashboard[/bold {s['accent']}] — {len(sessions)} agent{'s' if len(sessions) != 1 else ''}")
        preamble_widget.display = True

        list_view = self.query_one("#choices", ListView)
        list_view.clear()

        import time as _time
        now = _time.time()
        narration_parts = [f"{len(sessions)} agent{'s' if len(sessions) != 1 else ''} active."]

        for i, sess in enumerate(sessions):
            # Determine status
            if sess.active:
                status_text = f"[{s['success']}]● choices[/{s['success']}]"
                status_narr = "has choices"
            elif sess.voice_recording:
                status_text = f"[{s['error']}]● recording[/{s['error']}]"
                status_narr = "recording"
            else:
                status_text = f"[{s['warning']}]◌ working[/{s['warning']}]"
                status_narr = "working"

            # Elapsed time
            elapsed = now - getattr(sess, 'last_tool_call', now)
            if elapsed < 60:
                time_str = f"{int(elapsed)}s"
            else:
                time_str = f"{int(elapsed)//60}m{int(elapsed)%60:02d}s"

            # Last speech
            last_speech = ""
            if sess.speech_log:
                last_speech = sess.speech_log[-1].text
                if len(last_speech) > 50:
                    last_speech = last_speech[:50] + "..."

            # Pending messages
            msgs = getattr(sess, 'pending_messages', [])
            msg_info = f" [{s['purple']}]{len(msgs)} msg[/{s['purple']}]" if msgs else ""

            label = f"{sess.name}  {status_text}  [{s['fg_dim']}]{time_str}[/{s['fg_dim']}]{msg_info}"
            summary = last_speech if last_speech else "[dim]no recent speech[/dim]"

            list_view.append(ChoiceItem(label, summary, index=i + 1, display_index=i))
            narration_parts.append(f"{sess.name}: {status_narr}, {time_str}.")

        list_view.display = True
        list_view.index = 0
        list_view.focus()

        # Narrate the dashboard
        self._tts.speak_async(" ".join(narration_parts))

    @_safe_action
    def action_agent_log(self) -> None:
        """Show the full speech log for the focused agent.

        Displays all speech entries in a scrollable list. Each entry
        is read aloud when highlighted. Press g or Escape to return.
        """
        # Toggle off if already in log viewer
        if getattr(self, '_log_viewer_mode', False):
            self._log_viewer_mode = False
            self._exit_settings()
            return

        session = self._focused()
        if session and (session.input_mode or session.voice_recording):
            return
        if self._in_settings or self._filter_mode:
            return

        if not session:
            self._tts.speak_async("No active session")
            return

        speech_log = getattr(session, 'speech_log', [])
        if not speech_log:
            self._tts.speak_async("No speech log for this session")
            return

        self._tts.stop()

        # Enter log viewer mode (uses settings infrastructure for modal display)
        self._in_settings = True
        self._setting_edit_mode = False
        self._spawn_options = None
        self._quick_action_options = None
        self._dashboard_mode = False
        self._log_viewer_mode = True

        s = self._cs

        preamble_widget = self.query_one("#preamble", Label)
        count = len(speech_log)
        preamble_widget.update(
            f"[bold {s['accent']}]Agent Log[/bold {s['accent']}] — "
            f"[{s['fg_dim']}]{session.name}[/{s['fg_dim']}] — "
            f"{count} entr{'y' if count == 1 else 'ies'} "
            f"[dim](g/esc to close)[/dim]"
        )
        preamble_widget.display = True

        list_view = self.query_one("#choices", ListView)
        list_view.clear()

        import time as _time

        for i, entry in enumerate(speech_log):
            # Format timestamp as relative time
            age = _time.time() - entry.timestamp
            if age < 60:
                time_str = f"{int(age)}s ago"
            elif age < 3600:
                time_str = f"{int(age)//60}m ago"
            else:
                time_str = f"{int(age)//3600}h{int(age)%3600//60:02d}m ago"

            # Truncate text for display
            text = entry.text
            display_text = text[:120] + ("..." if len(text) > 120 else "")

            # Priority indicator
            priority_mark = f"[{s['error']}]![/{s['error']}] " if entry.priority >= 1 else ""

            label = f"{priority_mark}[{s['fg_dim']}]{time_str}[/{s['fg_dim']}]  {display_text}"
            list_view.append(ChoiceItem(label, "", index=i + 1, display_index=i))

        list_view.display = True
        # Start at the bottom (most recent)
        list_view.index = max(0, len(speech_log) - 1)
        list_view.focus()

        self._tts.speak_async(f"Agent log. {count} entries. Most recent shown.")

    @_safe_action
    def action_show_help(self) -> None:
        """Show a help screen with all keyboard shortcuts.

        Displays shortcuts in a scrollable list with descriptions.
        Each shortcut is read aloud when highlighted.
        Press ? or Escape to return.
        """
        # Toggle off if already showing
        if getattr(self, '_help_mode', False):
            self._help_mode = False
            self._exit_settings()
            return

        session = self._focused()
        if session and (session.input_mode or session.voice_recording):
            return
        if self._in_settings or self._filter_mode:
            return

        self._tts.stop()

        # Enter help mode
        self._in_settings = True
        self._setting_edit_mode = False
        self._spawn_options = None
        self._quick_action_options = None
        self._dashboard_mode = False
        self._log_viewer_mode = False
        self._help_mode = True

        s = self._cs

        # Get current key bindings
        kb = self._config.key_bindings if self._config else {}

        shortcuts = [
            (kb.get("cursorDown", "j") + "/" + kb.get("cursorUp", "k"), "Navigate choices up and down"),
            ("Enter", "Select the highlighted choice or stop recording"),
            ("1-9", "Instantly select a choice by its number"),
            (kb.get("undoSelection", "u"), "Undo the last selection and go back"),
            (kb.get("filterChoices", "/"), "Filter choices by typing"),
            (kb.get("voiceInput", "space"), "Toggle voice recording for speech input"),
            (kb.get("freeformInput", "i"), "Type a freeform text reply"),
            (kb.get("queueMessage", "m"), "Queue a message for the agent"),
            (kb.get("settings", "s"), "Open the settings menu"),
            (kb.get("nextTab", "l") + "/" + kb.get("prevTab", "h"), "Switch between agent tabs"),
            (kb.get("nextChoicesTab", "n"), "Jump to next tab with active choices"),
            (kb.get("spawnAgent", "t"), "Spawn a new Claude Code agent"),
            (kb.get("quickActions", "x"), "Run a quick action macro"),
            (kb.get("conversationMode", "c"), "Toggle continuous voice conversation mode"),
            (kb.get("dashboard", "d"), "Show dashboard overview of all agents"),
            (kb.get("agentLog", "g"), "View scrollable speech log for focused agent"),
            ("?", "Show this help screen"),
            (kb.get("replayPrompt", "p"), "Replay the preamble text"),
            (kb.get("hotReload", "r"), "Hot reload code and config"),
            (kb.get("quit", "q"), "Quit io-mcp"),
        ]

        preamble_widget = self.query_one("#preamble", Label)
        preamble_widget.update(
            f"[bold {s['accent']}]Keyboard Shortcuts[/bold {s['accent']}] "
            f"[dim](?/esc to close)[/dim]"
        )
        preamble_widget.display = True

        list_view = self.query_one("#choices", ListView)
        list_view.clear()

        for i, (key, desc) in enumerate(shortcuts):
            label = f"[bold {s['accent']}]{key}[/bold {s['accent']}]  {desc}"
            list_view.append(ChoiceItem(label, "", index=i + 1, display_index=i))

        list_view.display = True
        list_view.index = 0
        list_view.focus()

        self._help_shortcuts = shortcuts
        self._tts.speak_async(f"Help. {len(shortcuts)} keyboard shortcuts. Scroll to hear each one.")

    @_safe_action
    def action_quick_actions(self) -> None:
        """Show quick action picker.

        Quick actions are configurable macros defined in .io-mcp.yml:
        - message: Queue a predefined message to the focused agent
        - command: Run a shell command and speak the result
        """
        session = self._focused()
        if session and (session.input_mode or session.voice_recording):
            return
        if self._in_settings or self._filter_mode:
            return

        if not self._config:
            self._tts.speak_async("No config loaded")
            return

        actions = self._config.quick_actions
        if not actions:
            self._tts.speak_async("No quick actions configured. Add quickActions to .io-mcp.yml")
            return

        self._tts.stop()

        # Build options from quick actions
        options = []
        for qa in actions:
            key = qa.get("key", "")
            label = qa.get("label", qa.get("value", "")[:30])
            action_type = qa.get("action", "message")
            value = qa.get("value", "")
            key_hint = f" [{key}]" if key else ""
            type_icon = "💬" if action_type == "message" else "⚡"
            options.append({
                "label": f"{type_icon} {label}{key_hint}",
                "summary": value[:60] if value else "",
                "_action": qa,
            })

        options.append({"label": "Cancel", "summary": "Go back", "_action": None})

        # Show as settings-style menu
        self._in_settings = True
        self._setting_edit_mode = False
        self._spawn_options = None
        self._quick_action_options = None
        self._dashboard_mode = False  # clear any spawn state
        self._quick_action_options = options

        preamble_widget = self.query_one("#preamble", Label)
        preamble_widget.update(f"[bold {self._cs['purple']}]Quick Actions[/bold {self._cs['purple']}]")
        preamble_widget.display = True

        list_view = self.query_one("#choices", ListView)
        list_view.clear()
        for i, opt in enumerate(options):
            list_view.append(ChoiceItem(
                opt["label"], opt.get("summary", ""),
                index=i + 1, display_index=i,
            ))
        list_view.display = True
        list_view.index = 0
        list_view.focus()

        self._tts.speak_async("Quick actions. Pick one to run.")

    def _execute_quick_action(self, action: dict) -> None:
        """Execute a quick action in a background thread."""
        if not action:
            self._exit_settings()
            return

        action_type = action.get("action", "message")
        value = action.get("value", "")
        label = action.get("label", "")

        if action_type == "message":
            # Queue message to focused agent
            session = self._focused()
            if session:
                msgs = getattr(session, 'pending_messages', None)
                if msgs is not None:
                    msgs.append(value)
                count = len(msgs) if msgs else 1
                self._tts.speak_async(f"Queued: {label}. {count} pending.")
            else:
                self._tts.speak_async("No active session to send message to")
            self._exit_settings()

        elif action_type == "command":
            self._tts.speak_async(f"Running: {label}")

            def _run():
                try:
                    result = subprocess.run(
                        value, shell=True, capture_output=True, text=True, timeout=60
                    )
                    output = result.stdout.strip() or result.stderr.strip()
                    if result.returncode == 0:
                        summary = output[:200] if output else "Done"
                        self._tts.speak_async(f"Done. {summary}")
                    else:
                        err = output[:100] if output else f"exit code {result.returncode}"
                        self._tts.speak_async(f"Failed: {err}")
                except subprocess.TimeoutExpired:
                    self._tts.speak_async("Command timed out after 60 seconds")
                except Exception as e:
                    self._tts.speak_async(f"Error: {str(e)[:80]}")

                self.call_from_thread(self._exit_settings)

            threading.Thread(target=_run, daemon=True).start()

        else:
            self._tts.speak_async(f"Unknown action type: {action_type}")
            self._exit_settings()


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
