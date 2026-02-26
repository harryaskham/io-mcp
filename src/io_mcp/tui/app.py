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

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.events import MouseScrollDown, MouseScrollUp
from textual.reactive import reactive
from textual.timer import Timer
from textual.widget import Widget
from textual.widgets import Footer, Header, Input, Label, ListItem, ListView, RichLog, Static

from ..session import Session, SessionManager, SpeechEntry, HistoryEntry, InboxItem
from ..settings import Settings
from ..tts import PORTAUDIO_LIB, TTSEngine, _find_binary
from .. import api as frontend_api
from ..notifications import (
    NotificationDispatcher, NotificationEvent, create_dispatcher,
)

from .themes import COLOR_SCHEMES, DEFAULT_SCHEME, get_scheme, build_css
from .widgets import ChoiceItem, InboxListItem, DwellBar, ManagedListView, TextInputModal, VOICE_REQUESTED, EXTRA_OPTIONS, PRIMARY_EXTRAS, SECONDARY_EXTRAS, MORE_OPTIONS_ITEM, _safe_action
from .views import ViewsMixin
from .voice import VoiceMixin
from .settings_menu import SettingsMixin

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from ..config import IoMcpConfig


# Alias for internal use
_build_css = build_css


# ─── Main TUI App ───────────────────────────────────────────────────────────

class IoMcpApp(ViewsMixin, VoiceMixin, SettingsMixin, App):
    """Textual app for io-mcp choice presentation with multi-session support."""

    CSS = _build_css(DEFAULT_SCHEME)

    BINDINGS = [
        Binding("j,down", "cursor_down", "Down", show=False),
        Binding("k,up", "cursor_up", "Up", show=False),
        Binding("enter", "select", "Select", show=True),
        Binding("i", "freeform_input", "Type reply", show=True),
        Binding("m", "queue_message", "Message", show=True),
        Binding("M", "voice_message", "Voice msg", show=False),
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
        Binding("x", "multi_select_toggle", "Multi", show=False),
        Binding("c", "toggle_conversation", "Chat", show=False),
        Binding("v", "pane_view", "Pane", show=False),
        Binding("b", "toggle_sidebar", "Sidebar", show=False),
        Binding("question_mark", "show_help", "Help", show=False),
        Binding("r", "hot_reload", "Refresh", show=False),
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
        multi_select_key = kb.get("multiSelect", "x")
        convo_key = kb.get("conversationMode", "c")
        pane_key = kb.get("paneView", "v")
        help_key = kb.get("help", "question_mark")
        reload_key = kb.get("refresh", kb.get("hotReload", "r"))
        quit_key = kb.get("quit", "q")

        voice_message_key = kb.get("voiceMessage", "M")

        self._bindings = [
            Binding(f"{down_key},down", "cursor_down", "Down", show=False),
            Binding(f"{up_key},up", "cursor_up", "Up", show=False),
            Binding(select_key, "select", "Select", show=True),
            Binding(freeform_key, "freeform_input", "Type reply", show=True),
            Binding(message_key, "queue_message", "Message", show=True),
            Binding(voice_key, "voice_input", "Voice", show=True),
            Binding(voice_message_key, "voice_message", "Voice msg", show=False),
            Binding(settings_key, "toggle_settings", "Settings", show=True),
            Binding(replay_key, "replay_prompt", "Replay", show=False),
            Binding(replay_all_key, "replay_prompt_full", "Replay all", show=False),
            Binding(next_tab_key, "next_tab", "Next tab", show=False),
            Binding(prev_tab_key, "prev_tab", "Prev tab", show=False),
            Binding(next_choices_key, "next_choices_tab", "Next choices", show=False),
            Binding(undo_key, "undo_selection", "Undo", show=False),
            Binding(filter_key, "filter_choices", "Filter", show=False),
            Binding(spawn_key, "spawn_agent", "New agent", show=False),
            Binding(multi_select_key, "multi_select_toggle", "Multi", show=False),
            Binding(convo_key, "toggle_conversation", "Chat", show=False),
            Binding(pane_key, "pane_view", "Pane", show=False),
            Binding(help_key, "show_help", "Help", show=False),
            Binding(reload_key, "hot_reload", "Refresh", show=False),
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
        self._message_target_session = None  # session to queue message to (inbox-aware)
        self._interrupt_mode = False  # True when sending directly to agent pane
        self._restart_requested = False

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

        # Extra options expand/collapse state
        self._extras_expanded = False

        # Dwell timer
        self._dwell_timer: Optional[Timer] = None
        self._dwell_start: float = 0.0

        # Flag: is foreground currently speaking (blocks bg playback)
        self._fg_speaking = False

        # Haptic feedback — disabled by default, enabled via config.haptic.enabled
        self._termux_vibrate = _find_binary("termux-vibrate")
        haptic_cfg = config.haptic_enabled if config else False
        self._haptic_enabled = haptic_cfg and self._termux_vibrate is not None

        # TTS deduplication — track last spoken text to avoid repeats
        self._last_spoken_text: str = ""

        # Daemon health status (rendered in tab bar RHS)
        self._daemon_status_text: str = ""
        self._daemon_check_running: bool = False  # guard against overlapping checks

        # PulseAudio auto-reconnect state
        self._pulse_was_ok: bool = True  # assume healthy at start
        self._pulse_reconnect_attempts: int = 0
        self._pulse_last_reconnect: float = 0.0

        # Filter mode
        self._filter_mode = False

        # Conversation mode — continuous voice back-and-forth
        self._conversation_mode = False

        # System logs mode (TUI errors, proxy logs, speech history)
        self._system_logs_mode = False

        # Help screen mode
        self._help_mode = False

        # Tab picker mode
        self._tab_picker_mode = False

        # Multi-select mode (toggle choices then confirm)
        self._multi_select_mode = False
        self._multi_select_checked: list[bool] = []

        # Inbox pane focus state (two-column layout)
        self._inbox_pane_focused = False
        self._inbox_scroll_index = 0  # cursor position in inbox list
        self._inbox_was_visible = False  # saved inbox state for message mode
        self._inbox_last_generation = -1  # tracks session._inbox_generation to skip no-op rebuilds
        self._inbox_collapsed = False  # user-toggled collapse state

        # Notification webhooks
        self._notifier = create_dispatcher(config)

    # ─── Helpers to get focused session ────────────────────────────

    def _focused(self) -> Optional[Session]:
        """Get the currently focused session."""
        return self.manager.focused()

    def _message_target(self) -> Optional["Session"]:
        """Get the session that should receive a queued message.

        If the inbox is visible and an item is highlighted, returns the
        session that owns that inbox item. Otherwise falls back to the
        active (focused) session. This ensures messages go to the agent
        the user is currently interacting with in the inbox.
        """
        if self._inbox_pane_visible():
            try:
                inbox_list = self.query_one("#inbox-list", ListView)
                if inbox_list.index is not None and inbox_list.index < len(inbox_list.children):
                    item = inbox_list.children[inbox_list.index]
                    if isinstance(item, InboxListItem) and item.session_id:
                        sess = self.manager.sessions.get(item.session_id)
                        if sess:
                            return sess
            except Exception:
                pass
        return self._focused()

    def _is_focused(self, session_id: str) -> bool:
        """Check if a session is the focused one."""
        return self.manager.active_session_id == session_id

    def _speak_ui(self, text: str) -> None:
        """Speak a UI message (settings, navigation, prompts) with optional separate voice.

        Uses tts.uiVoice from config if set, otherwise falls back to the
        regular voice. This keeps UI narration distinct from agent speech.
        """
        voice_ov = None
        if self._config:
            ui_voice = self._config.tts_ui_voice
            # Only override if uiVoice is explicitly set and different from default
            if ui_voice and ui_voice != self._config.tts_voice:
                voice_ov = ui_voice
        self._tts.speak_async(text, voice_override=voice_ov)

    @work(thread=True, exit_on_error=False, group="pregenerate")
    def _pregenerate_worker(self, texts: list[str]) -> None:
        """Worker: pregenerate TTS clips in background thread."""
        self._tts.pregenerate(texts)

    def _ensure_main_content_visible(self, show_inbox: bool = False) -> None:
        """Ensure the #main-content container is visible.

        Called before showing the #choices list in any context (settings,
        etc.) since #choices is now nested inside #main-content > #choices-panel.

        Args:
            show_inbox: If True, also update and show the inbox list
                       (unless user has collapsed it). If False, hide
                       the inbox list (for modal views).
        """
        try:
            self.query_one("#main-content").display = True
            if show_inbox and not self._inbox_collapsed:
                self._update_inbox_list()
            else:
                self.query_one("#inbox-list").display = False
        except Exception:
            pass

    # ─── Haptic feedback ────────────────────────────────────────────

    def _vibrate(self, duration_ms: int = 30) -> None:
        """Trigger haptic feedback via termux-vibrate (fire-and-forget).

        Uses termux-exec if available (needed on Nix-on-Droid/proot),
        otherwise falls back to direct termux-vibrate.
        Runs as a Textual worker to avoid blocking the event loop
        (subprocess.Popen on proot can take 100ms+).

        Args:
            duration_ms: Vibration duration in milliseconds.
                         30ms for scroll, 100ms for selection.
        """
        if not self._haptic_enabled:
            return
        # Use cached binary path (found at __init__ or first call)
        cmd = getattr(self, '_vibrate_cmd', None)
        if cmd is None:
            # Build and cache the command template on first use
            termux_exec = getattr(self, '_cached_termux_exec', None)
            if termux_exec is None:
                termux_exec = _find_binary("termux-exec")
                self._cached_termux_exec = termux_exec or ""
            if termux_exec:
                cmd = [termux_exec, "termux-vibrate", "-d", "DUR", "-f"]
            elif self._termux_vibrate:
                cmd = [self._termux_vibrate, "-d", "DUR", "-f"]
            else:
                self._vibrate_cmd = []  # no vibration available
                return
            self._vibrate_cmd = cmd
        if not cmd:
            return
        # Replace placeholder duration and fire in background
        actual_cmd = [c if c != "DUR" else str(duration_ms) for c in cmd]
        self._vibrate_worker(actual_cmd)

    @work(thread=True, exit_on_error=False, group="vibrate")
    def _vibrate_worker(self, cmd: list[str]) -> None:
        """Worker: run vibration subprocess in background thread."""
        try:
            subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            pass

    def _vibrate_pattern(self, pattern: str = "pulse") -> None:
        """Play a vibration pattern for semantic haptic feedback.

        Patterns:
            pulse: Three quick buzzes (new choices)
            heavy: Long-short-long (selection confirmed)
            attention: Rapid SOS-like (urgent/error)
            heartbeat: Gentle double-tap (ambient update)
        """
        if not self._haptic_enabled:
            return

        patterns = {
            "pulse": [30, 80, 30, 80, 30],        # buzz-gap-buzz-gap-buzz
            "heavy": [100, 60, 40, 60, 100],       # heavy-gap-light-gap-heavy
            "attention": [50, 40, 50, 40, 50, 40, 120],  # rapid bursts + long
            "heartbeat": [20, 100, 40],             # soft double-tap
        }

        durations = patterns.get(pattern, patterns["pulse"])
        self._vibrate_pattern_worker(durations)

    @work(thread=True, exit_on_error=False, group="vibrate_pattern")
    def _vibrate_pattern_worker(self, durations: list[int]) -> None:
        """Worker: play vibration pattern in background thread."""
        import time as _t
        for i, ms in enumerate(durations):
            if i % 2 == 0:
                # Vibrate
                self._vibrate(ms)
            else:
                # Pause
                _t.sleep(ms / 1000.0)

    # ─── Widget composition ────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield Header(name="io-mcp", show_clock=False)
        with Horizontal(id="tab-bar"):
            yield Static("", id="tab-bar-left")
            yield Static("", id="tab-bar-right")
        yield Static("", id="daemon-status")
        status_text = "[dim]Ready — demo mode[/dim]" if self._demo else "[dim]Waiting for agent...[/dim]"
        yield Label(status_text, id="status")
        yield Label("", id="agent-activity")
        yield Vertical(id="speech-log")
        with Horizontal(id="main-content"):
            yield ManagedListView(id="inbox-list")
            with Vertical(id="choices-panel"):
                yield Label("", id="preamble")
                yield ManagedListView(id="choices")
                yield DwellBar(id="dwell-bar")
        yield RichLog(id="pane-view", markup=False, highlight=False, auto_scroll=True, max_lines=200)
        yield Input(placeholder="Filter choices...", id="filter-input")
        yield Static("[dim]↕[/dim] Scroll  [dim]⏎[/dim] Select  [dim]x[/dim] Multi  [dim]u[/dim] Undo  [dim]i[/dim] Type  [dim]m[/dim] Msg  [dim]␣[/dim] Voice  [dim]/[/dim] Filter  [dim]v[/dim] Pane  [dim]s[/dim] Settings  [dim]q[/dim] Back/Quit", id="footer-help")

    def on_mount(self) -> None:
        self.title = "io-mcp"
        self.sub_title = ""
        # Tab bar always visible — shows branding or agent names
        self._update_tab_bar()
        self.query_one("#preamble").display = False
        self.query_one("#choices").display = False
        self.query_one("#dwell-bar").display = False
        self.query_one("#speech-log").display = False
        self.query_one("#pane-view").display = False
        self.query_one("#main-content").display = False
        self.query_one("#inbox-list").display = False

        # Start periodic session cleanup (every 60 seconds, 5 min timeout)
        self._cleanup_timer = self.set_interval(60, self._cleanup_stale_sessions)
        # Heartbeat: check every 15s if agent has been silent too long
        self._heartbeat_timer = self.set_interval(15, self._check_heartbeat)
        # Daemon health check: every 30s update status indicators
        self._daemon_health_timer = self.set_interval(30, self._update_daemon_status)
        # Agent health monitor: check every 30s if agents are stuck/crashed
        health_interval = 30.0
        if self._config and hasattr(self._config, 'health_check_interval'):
            health_interval = self._config.health_check_interval
        self._agent_health_timer = self.set_interval(health_interval, self._check_agent_health)
        # Initial health check
        self._update_daemon_status()

    def watch_focused(self, focused: Widget | None) -> None:
        """Keep _inbox_pane_focused in sync when widget focus changes.

        This fires whenever Textual's focus changes (Tab key, click, etc.),
        ensuring the logical inbox/choices state matches actual widget focus.
        """
        self._sync_inbox_focus_from_widget()

    def _safe_call(self, callback, *args) -> bool:
        """Call callback on the Textual event loop, swallowing 'App is not running'.

        Returns True if the call succeeded, False if the app was not running.
        Use this for non-critical UI updates (tab bar, speech log, etc.) that
        should not crash the calling thread during TUI restarts.
        """
        try:
            if args:
                self.call_from_thread(callback, *args)
            else:
                self.call_from_thread(callback)
            return True
        except RuntimeError:
            return False

    def _touch_session(self, session: Session) -> None:
        """Update last_activity, safe for old Session objects without the field."""
        try:
            session.last_activity = time.time()
        except AttributeError:
            pass

    def _update_daemon_status(self) -> None:
        """Check proxy/backend/API/PulseAudio health and store as a status string.

        Status is displayed in the right side of the tab bar via _update_tab_bar.
        Runs health checks via a Textual worker to avoid blocking the TUI.

        When PulseAudio goes down and config.pulseAudio.autoReconnect is True,
        attempts auto-reconnect via the TTS engine's reconnect_pulse() method.
        """
        # Guard: skip if a previous check is still running
        if self._daemon_check_running:
            return

        self._daemon_check_worker()

    @work(thread=True, exit_on_error=False, name="daemon_status", exclusive=True)
    def _daemon_check_worker(self) -> None:
        """Worker: run daemon health check in background thread."""
        self._daemon_check_running = True
        try:
            self._do_daemon_check()
        finally:
            self._daemon_check_running = False

    def _do_daemon_check(self) -> None:
        """Actual daemon health check logic, runs in background thread."""
        import urllib.request
        import urllib.error
        import shutil

        # Check PulseAudio via pactl info
        pls_ok = False
        pactl = shutil.which("pactl")
        if pactl:
            try:
                env = os.environ.copy()
                env["PULSE_SERVER"] = os.environ.get("PULSE_SERVER", "127.0.0.1")
                result = subprocess.run(
                    [pactl, "info"],
                    env=env, capture_output=True, timeout=2,
                )
                pls_ok = result.returncode == 0
            except Exception:
                pass

        # ── PulseAudio auto-reconnect ───────────────────────────
        if not pls_ok and self._pulse_was_ok:
            # Transition from OK → down: attempt reconnect
            self._try_pulse_reconnect()
        elif not pls_ok and not self._pulse_was_ok:
            # Still down: retry if cooldown has elapsed
            self._try_pulse_reconnect()
        elif pls_ok and not self._pulse_was_ok:
            # Recovered! Reset counters
            self._pulse_reconnect_attempts = 0
            self._pulse_last_reconnect = 0.0
            try:
                self._tts.play_chime("success")
            except Exception:
                pass
            try:
                with open("/tmp/io-mcp-tui-error.log", "a") as f:
                    f.write(f"\n--- PulseAudio recovered ---\n")
            except Exception:
                pass
            # Notify recovery
            try:
                self._notifier.notify(NotificationEvent(
                    event_type="pulse_recovered",
                    title="PulseAudio recovered",
                    message="PulseAudio connection restored.",
                    priority=2,
                    tags=["loud_sound", "pulse_recovered"],
                ))
            except Exception:
                pass

        self._pulse_was_ok = pls_ok

        # Check proxy via PID file
        proxy_ok = False
        try:
            with open("/tmp/io-mcp-server.pid", "r") as f:
                pid = int(f.read().strip())
            os.kill(pid, 0)
            proxy_ok = True
        except (FileNotFoundError, ValueError, ProcessLookupError, PermissionError):
            pass

        # Check backend /health — try both 127.0.0.1 and localhost
        backend_ok = False
        for host in ("127.0.0.1", "localhost"):
            if backend_ok:
                break
            try:
                req = urllib.request.Request(f"http://{host}:8446/health", method="GET")
                with urllib.request.urlopen(req, timeout=2) as resp:
                    backend_ok = resp.status == 200
            except Exception:
                pass

        # Check Android API /health — try both 127.0.0.1 and localhost
        api_ok = False
        for host in ("127.0.0.1", "localhost"):
            if api_ok:
                break
            try:
                req = urllib.request.Request(f"http://{host}:8445/api/health", method="GET")
                with urllib.request.urlopen(req, timeout=2) as resp:
                    api_ok = resp.status == 200
            except Exception:
                pass

        # Check termux-exec daemon via 'termux-exec true'
        tx_ok = False
        termux_exec = shutil.which("termux-exec")
        if termux_exec:
            try:
                result = subprocess.run(
                    [termux_exec, "true"],
                    capture_output=True, timeout=2,
                )
                tx_ok = result.returncode == 0
            except Exception:
                pass

        # Build compact status text for tab bar RHS
        s = self._cs

        def _dot(ok: bool) -> str:
            color = s['success'] if ok else s['error']
            return f"[{color}]o[/{color}]"

        parts = [
            f"{_dot(pls_ok)}pls",
            f"{_dot(proxy_ok)}mcp",
            f"{_dot(backend_ok)}tui",
            f"{_dot(api_ok)}api",
            f"{_dot(tx_ok)}tx",
        ]

        self._daemon_status_text = " ".join(parts)

        try:
            self.call_from_thread(self._update_tab_bar)
        except Exception:
            pass

    def _try_pulse_reconnect(self) -> None:
        """Attempt PulseAudio auto-reconnect if enabled and within limits.

        Respects config settings for max attempts and cooldown period.
        Logs all attempts and plays appropriate chimes on success/failure.
        Sends notification webhooks on failure and recovery.
        Provides specific recovery steps when all attempts are exhausted.
        Auto-resets attempt counter after a backoff period (5x cooldown)
        so reconnection is retried periodically even after initial exhaustion.
        """
        # Check if auto-reconnect is enabled
        if self._config and not self._config.pulse_auto_reconnect:
            return

        max_attempts = 3
        cooldown = 30.0
        if self._config:
            max_attempts = self._config.pulse_max_reconnect_attempts
            cooldown = self._config.pulse_reconnect_cooldown

        now = time.time()

        # Check if we've exceeded max attempts
        if self._pulse_reconnect_attempts >= max_attempts:
            return

        # Check cooldown
        if now - self._pulse_last_reconnect < cooldown:
            return

        self._pulse_reconnect_attempts += 1
        self._pulse_last_reconnect = now
        attempt = self._pulse_reconnect_attempts

        try:
            with open("/tmp/io-mcp-tui-error.log", "a") as f:
                f.write(
                    f"\n--- PulseAudio reconnect attempt {attempt}/{max_attempts} ---\n"
                )
        except Exception:
            pass

        # Play warning chime to indicate reconnect attempt
        try:
            self._tts.play_chime("warning")
        except Exception:
            pass

        # Attempt reconnection via TTS engine
        success = False
        diagnostic_info = ""
        try:
            success, diagnostic_info = self._tts.reconnect_pulse()
        except Exception:
            pass

        if success:
            self._pulse_was_ok = True
            self._pulse_reconnect_attempts = 0
            self._pulse_last_reconnect = 0.0
            try:
                self._tts.play_chime("success")
            except Exception:
                pass
            try:
                with open("/tmp/io-mcp-tui-error.log", "a") as f:
                    f.write(f"  → PulseAudio reconnected successfully!\n")
                    if diagnostic_info:
                        f.write(f"    Diagnostics: {diagnostic_info}\n")
            except Exception:
                pass
            # Notify recovery
            try:
                self._notifier.notify(NotificationEvent(
                    event_type="pulse_recovered",
                    title="PulseAudio recovered",
                    message=f"PulseAudio reconnected on attempt {attempt}.",
                    priority=2,
                    tags=["loud_sound", "pulse_recovered"],
                    extra={"diagnostics": diagnostic_info},
                ))
            except Exception:
                pass
        else:
            remaining = max_attempts - attempt
            try:
                with open("/tmp/io-mcp-tui-error.log", "a") as f:
                    f.write(
                        f"  → PulseAudio reconnect failed "
                        f"({remaining} attempts remaining)\n"
                    )
                    if diagnostic_info:
                        f.write(f"    Diagnostics: {diagnostic_info}\n")
            except Exception:
                pass

            # Send notification on each failure
            try:
                self._notifier.notify(NotificationEvent(
                    event_type="pulse_down",
                    title="PulseAudio down",
                    message=(
                        f"PulseAudio reconnect attempt {attempt}/{max_attempts} failed. "
                        f"{remaining} attempts remaining."
                    ),
                    priority=4,
                    tags=["mute", "pulse_down"],
                    extra={"diagnostics": diagnostic_info,
                           "attempt": attempt,
                           "max_attempts": max_attempts},
                ))
            except Exception:
                pass

            # When all attempts exhausted, speak recovery steps
            if remaining == 0:
                self._pulse_recovery_exhausted(diagnostic_info)

    def _pulse_recovery_exhausted(self, diagnostic_info: str = "") -> None:
        """Handle exhausted PulseAudio reconnect attempts.

        Speaks specific recovery steps, logs them, and sends a high-priority
        notification with actionable guidance so the user knows exactly what
        to do to restore audio.
        """
        try:
            steps = self._tts.pulse_recovery_steps()
        except Exception:
            steps = ["Restart io-mcp TUI to reset audio subsystem"]

        steps_text = "\n".join(f"  {i+1}. {s}" for i, s in enumerate(steps))
        log_msg = (
            f"\n--- PulseAudio auto-reconnect exhausted ---\n"
            f"Recovery steps:\n{steps_text}\n"
        )
        if diagnostic_info:
            log_msg += f"Last diagnostics: {diagnostic_info}\n"

        try:
            with open("/tmp/io-mcp-tui-error.log", "a") as f:
                f.write(log_msg)
        except Exception:
            pass

        # Speak recovery guidance (using termux fallback if PulseAudio is down)
        try:
            spoken_steps = ". ".join(steps[:3])
            self._tts.speak_async(
                f"PulseAudio recovery failed. Try: {spoken_steps}"
            )
        except Exception:
            pass

        # Play error chime
        try:
            self._tts.play_chime("error")
        except Exception:
            pass

        # Haptic feedback
        try:
            self._vibrate_pattern("attention")
        except Exception:
            pass

        # High-priority notification with recovery steps
        try:
            self._notifier.notify(NotificationEvent(
                event_type="pulse_down",
                title="PulseAudio recovery exhausted",
                message=(
                    "All auto-reconnect attempts failed. "
                    "Manual intervention required.\n\n"
                    "Recovery steps:\n" + "\n".join(f"• {s}" for s in steps)
                ),
                priority=5,
                tags=["rotating_light", "pulse_down"],
                extra={
                    "diagnostics": diagnostic_info,
                    "recovery_steps": steps,
                },
            ))
        except Exception:
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

    @work(thread=True, exit_on_error=False, name="agent_health_check", exclusive=True)
    def _check_agent_health(self) -> None:
        """Monitor agent health in a Textual worker.

        Runs subprocess calls (tmux pane checks, SSH) in a thread to avoid
        blocking the event loop. UI updates are dispatched via call_from_thread.
        """
        self._check_agent_health_inner()

    def _check_agent_health_inner(self) -> None:
        """Inner health check — runs in a background thread.

        For each session, checks:
        1. Time since last tool call — if too old while NOT presenting choices,
           the agent may be stuck (stuck in a loop, waiting on IO, etc.)
        2. Whether the agent's tmux pane is still alive (if registered with pane info)

        Health states:
            healthy:      last tool call is recent, pane alive (or unknown)
            warning:      no tool call for warningThresholdSecs (default 5 min)
            unresponsive: no tool call for unresponsiveThresholdSecs (default 10 min)
                          OR tmux pane is confirmed dead

        On state transition to warning/unresponsive:
            - Plays the "warning" or "error" chime
            - Speaks an alert (once per escalation level)
            - Triggers haptic "attention" feedback
            - Updates the tab bar with a visual indicator

        Health resets to "healthy" when the agent makes a new tool call
        (tracked via session.last_tool_call timestamp reset in server.py).
        """
        if self._config and hasattr(self._config, 'health_monitor_enabled'):
            if not self._config.health_monitor_enabled:
                return

        warning_threshold = 300.0
        unresponsive_threshold = 600.0
        check_tmux = True

        if self._config:
            if hasattr(self._config, 'health_warning_threshold'):
                warning_threshold = self._config.health_warning_threshold
            if hasattr(self._config, 'health_unresponsive_threshold'):
                unresponsive_threshold = self._config.health_unresponsive_threshold
            if hasattr(self._config, 'health_check_tmux_pane'):
                check_tmux = self._config.health_check_tmux_pane

        now = time.time()
        tab_bar_dirty = False

        for session in self.manager.all_sessions():
            # Only monitor sessions that have actually registered/connected
            last_call = getattr(session, 'last_tool_call', 0)
            if last_call == 0:
                continue

            # If agent is actively waiting for user selection, it's healthy —
            # it made a successful present_choices() call
            if session.active:
                if session.health_status != "healthy":
                    session.health_status = "healthy"
                    session.health_alert_spoken = False
                    tab_bar_dirty = True
                continue

            elapsed = now - last_call
            old_status = session.health_status

            # ── Check tmux pane liveness ─────────────────────────
            pane_dead = False
            if check_tmux:
                pane_dead = self._is_tmux_pane_dead(session)

            # ── Determine new health status ───────────────────────
            if pane_dead:
                new_status = "unresponsive"
            elif elapsed >= unresponsive_threshold:
                new_status = "unresponsive"
            elif elapsed >= warning_threshold:
                new_status = "warning"
            else:
                new_status = "healthy"

            # ── Reset alert flag when recovering to healthy ───────
            if new_status == "healthy" and old_status != "healthy":
                session.health_status = "healthy"
                session.health_alert_spoken = False
                tab_bar_dirty = True
                continue

            # ── Handle escalating alert on new bad status ─────────
            if new_status != old_status or (new_status != "healthy" and not session.health_alert_spoken):
                session.health_status = new_status
                tab_bar_dirty = True

                if new_status == "unresponsive" and not session.health_alert_spoken:
                    session.health_alert_spoken = True
                    self._fire_health_alert(session, "unresponsive", pane_dead, elapsed)
                elif new_status == "warning" and not session.health_alert_spoken:
                    session.health_alert_spoken = True
                    self._fire_health_alert(session, "warning", pane_dead, elapsed)
            elif new_status == old_status and new_status != "healthy":
                # Status unchanged and still bad — ensure flag is set
                session.health_alert_spoken = True

        # ── Auto-prune dead sessions ─────────────────────────────
        # Heuristics for detecting dead sessions:
        # 1. Dead tmux pane (immediate — don't wait for unresponsive timer)
        # 2. Unresponsive sessions without tmux info (no way to verify)
        dead_sessions = []

        for session in self.manager.all_sessions():
            if session.session_id == self.manager.active_session_id:
                continue  # never auto-prune focused session
            if session.active:
                continue  # has pending choices

            # Heuristic 1: Dead tmux pane — immediate removal
            pane_dead = self._is_tmux_pane_dead(session)
            if pane_dead:
                dead_sessions.append((session, "dead tmux pane"))
                continue

            # Heuristic 2: Unresponsive without tmux info (can't verify liveness)
            if session.health_status == "unresponsive":
                has_tmux = bool(getattr(session, 'tmux_pane', ''))
                if not has_tmux:
                    dead_sessions.append((session, "unresponsive, no tmux"))

        for session, reason in dead_sessions:
            name = session.name
            self.on_session_removed(session.session_id)
            tab_bar_dirty = True
            try:
                self._speak_ui(f"Removed dead session {name}")
            except Exception:
                pass

        if tab_bar_dirty:
            try:
                self.call_from_thread(self._update_tab_bar)
            except Exception:
                pass

    def _is_tmux_pane_dead(self, session: "Session") -> bool:
        """Check if a session's registered tmux pane has exited.

        Returns True if the pane is confirmed dead (process exited or doesn't exist).
        Returns False if the pane is alive, not registered, or check fails.

        Supports both local and remote tmux checks. For remote agents,
        uses SSH to check the tmux pane on the remote host.

        Uses `tmux display-message -p -t <pane_id> "#{pane_dead}"` which outputs
        "1" if the pane's shell has exited (pane is in a "dead" state), "0" otherwise.
        """
        pane = getattr(session, 'tmux_pane', '')
        tmux_session_name = getattr(session, 'tmux_session', '')

        if not pane and not tmux_session_name:
            return False  # no tmux info, can't check

        try:
            target = pane if pane else tmux_session_name
            hostname = getattr(session, 'hostname', '')

            # Check if this is a remote agent
            is_remote = False
            if hostname:
                local_hostname = os.uname().nodename
                is_remote = hostname not in ("", "localhost", local_hostname)

            if is_remote:
                cmd = [
                    "ssh", "-o", "ConnectTimeout=2", "-o", "StrictHostKeyChecking=no",
                    hostname,
                    f"tmux display-message -p -t {target} '#{{pane_dead}}'",
                ]
            else:
                cmd = ["tmux", "display-message", "-p", "-t", target, "#{pane_dead}"]

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=2,
            )
            if result.returncode != 0:
                # tmux command failed — pane/session doesn't exist
                return True
            return result.stdout.strip() == "1"
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            return False  # tmux not available or timeout — don't flag as dead

    def _fire_health_alert(self, session: "Session", status: str,
                           pane_dead: bool, elapsed: float) -> None:
        """Play chime + speak alert for a health status change.

        Runs in the health check thread (background timer), so uses
        non-blocking speak_async and fire-and-forget chime/haptic.
        """
        name = session.name
        minutes = int(elapsed) // 60
        secs = int(elapsed) % 60
        if minutes > 0:
            time_str = f"{minutes} minute{'s' if minutes != 1 else ''}"
        else:
            time_str = f"{secs} second{'s' if secs != 1 else ''}"

        if pane_dead:
            msg = f"{name} appears to have crashed. The tmux pane is no longer alive."
            chime = "error"
        elif status == "unresponsive":
            msg = f"{name} is unresponsive. No activity for {time_str}."
            chime = "error"
        else:
            msg = f"{name} may be stuck. No activity for {time_str}."
            chime = "warning"

        # Audio cue
        try:
            self._tts.play_chime(chime)
        except Exception:
            pass

        # Haptic feedback
        try:
            self._vibrate_pattern("attention")
        except Exception:
            pass

        # Voice alert
        try:
            self._tts.speak_async(msg)
        except Exception:
            pass

        # Send notification webhook
        try:
            event_type = f"health_{status}"
            self._notifier.notify(NotificationEvent(
                event_type=event_type,
                title=f"Agent {status}: {name}",
                message=msg,
                session_name=name,
                session_id=session.session_id,
                priority=5 if status == "unresponsive" else 4,
                tags=["skull" if pane_dead else "warning", status],
            ))
        except Exception:
            pass

    # Thinking-out-loud filler phrases for ambient updates
    _THINKING_PHRASES = [
        "Hmm, still thinking",
        "Let me see",
        "One moment",
        "Working on it",
        "Hmm, let me figure this out",
        "Just a sec",
        "Bear with me",
        "Almost there, I think",
        "Hmm",
        "Let me check something",
        "Hold on",
        "Huh, interesting",
        "Thinking",
        "One sec",
        "Mm, working on it",
    ]

    def _check_heartbeat(self) -> None:
        """Ambient mode: speak escalating status updates during agent silence.

        Tracks elapsed time since the last MCP tool call and speaks
        progressively more informative updates:

        1st: Random thinking-out-loud phrase
        2nd+: "Still working, N minutes in. Last update: [text]"

        Configurable via config.ambient.{enabled, initialDelaySecs, repeatIntervalSecs}.
        Resets when the agent makes its next MCP tool call.
        """
        import time as _time
        import random

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
            # First ambient update: thinking-out-loud filler
            if elapsed >= initial_delay:
                session.ambient_count = 1
                self._tts.play_chime("heartbeat") if hasattr(self._tts, 'play_chime') else None
                self._vibrate_pattern("heartbeat")
                phrase = random.choice(self._THINKING_PHRASES)
                self._speak_ui(phrase)
                self._update_ambient_indicator(session, elapsed)
        else:
            # Subsequent updates: exponential backoff after 4th update
            # 1st repeat at initial + repeat
            # 2nd at initial + 2*repeat
            # 3rd at initial + 3*repeat
            # 4th+ at initial + 3*repeat + (n-3)*repeat*2 (doubles spacing)
            if ambient_count <= 3:
                next_time = initial_delay + (ambient_count * repeat_interval)
            else:
                # Exponential backoff: each subsequent gap doubles
                base = initial_delay + (3 * repeat_interval)
                extra = sum(repeat_interval * (2 ** (i - 3)) for i in range(3, ambient_count))
                next_time = base + extra

            if elapsed >= next_time:
                session.ambient_count = ambient_count + 1
                minutes = int(elapsed) // 60
                last_text = ""
                if session.speech_log:
                    last_text = session.speech_log[-1].text
                    if len(last_text) > 60:
                        last_text = last_text[:60]

                last_tool = getattr(session, 'last_tool_name', '')

                # Mix thinking phrases with status info
                prefix = random.choice([
                    "Still at it.",
                    "Hmm, still going.",
                    "Working away.",
                    "Still crunching.",
                    "Chipping away.",
                    "Still on it.",
                    "Plugging along.",
                ])

                parts = [prefix]
                if minutes >= 1:
                    parts.append(f"{minutes} {'minute' if minutes == 1 else 'minutes'} in.")
                if last_tool:
                    parts.append(f"Last tool: {last_tool}.")
                if last_text:
                    parts.append(f"Last said: {last_text}")

                msg = " ".join(parts)

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
            if last_text:
                activity.update(f"[bold {self._cs['warning']}]~[/bold {self._cs['warning']}] Working ({time_str}){tool_info} -- {last_text}")
            else:
                activity.update(f"[bold {self._cs['warning']}]~[/bold {self._cs['warning']}] Working ({time_str}){tool_info}")
            activity.display = True
        except Exception:
            pass

    # ─── Tab bar rendering ─────────────────────────────────────────

    def _update_tab_bar(self) -> None:
        """Update the tab bar display.

        Two-section layout:
        - Left: agent tabs/branding (grows, wraps to multiple lines)
        - Right: daemon status indicators + inbox count (fixed)
        """
        try:
            tab_left = self.query_one("#tab-bar-left", Static)
            tab_right = self.query_one("#tab-bar-right", Static)
        except Exception:
            return

        s = get_scheme(getattr(self, '_color_scheme', DEFAULT_SCHEME))

        # ── Right side: status indicators + inbox ──
        rhs_parts = []
        # Inbox queue count across all sessions
        total_inbox = sum(
            sess.inbox_choices_count()
            for sess in self.manager.all_sessions()
        )
        if total_inbox > 0:
            rhs_parts.append(f"[bold {s['accent']}]q:{total_inbox}[/bold {s['accent']}]")
        # Daemon health dots
        daemon_status = getattr(self, '_daemon_status_text', '')
        if daemon_status:
            rhs_parts.append(daemon_status)
        tab_right.update(" ".join(rhs_parts) if rhs_parts else "")

        # ── Left side: agent tabs/branding ──
        if self.manager.count() <= 0:
            lhs = f"[bold {s['accent']}]io-mcp[/bold {s['accent']}]  [dim]waiting for agent...[/dim]"
        elif self.manager.count() == 1:
            session = self._focused()
            if session:
                name = session.name
                health = getattr(session, 'health_status', 'healthy')
                health_icon = ""
                if session.active:
                    inbox_count = session.inbox_choices_count()
                    if inbox_count > 1:
                        health_icon = f" [{s['success']}]o+{inbox_count - 1}[/{s['success']}]"
                    else:
                        health_icon = f" [{s['success']}]o[/{s['success']}]"
                elif health == "warning":
                    health_icon = f" [{s['warning']}]![/{s['warning']}]"
                elif health == "unresponsive":
                    health_icon = f" [{s['error']}]x[/{s['error']}]"
                lhs = f"[bold {s['accent']}]{name}[/bold {s['accent']}]{health_icon}"
            else:
                lhs = f"[bold {s['accent']}]io-mcp[/bold {s['accent']}]"
        else:
            lhs = self.manager.tab_bar_text(
                accent=s['accent'],
                success=s['success'],
                warning=s['warning'],
                error=s['error'],
            )

        tab_left.update(lhs)

    # ─── Speech log rendering ──────────────────────────────────────

    def _update_speech_log(self) -> None:
        """Update the speech log display and agent activity indicator.

        When the agent is working (no active choices), shows a clean
        waiting view with essential keyboard shortcuts in the right pane.
        """
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
        # Only show if ambient mode is enabled
        ambient_on = self._config.ambient_enabled if self._config else False
        if activity and session.speech_log and ambient_on:
            last = session.speech_log[-1].text
            activity.update(f"[bold {self._cs['blue']}]>[/bold {self._cs['blue']}] {last}")
            activity.display = True
        elif activity:
            activity.display = False

        # If agent is NOT presenting choices, show inbox waiting view
        # Rate limit: only update if enough time has passed (avoid flooding UI)
        if not session.active and not self._in_settings:
            import time as _time
            now = _time.time()
            last_feed = getattr(self, '_last_feed_update', 0)
            if now - last_feed > 1.0:  # At most once per second
                self._last_feed_update = now
                self._show_waiting_with_shortcuts(session)
            # Hide the small speech log — waiting view covers it
            log_widget.display = False
            return

        # Show last 5 speech entries (when choices ARE displayed)
        recent = session.speech_log[-5:]
        if not recent:
            log_widget.display = False
            return

        for entry in recent:
            label = Label(f"[dim]  |[/dim] {entry.text}", classes="speech-entry")
            log_widget.mount(label)
        log_widget.display = True

    # ─── Choice resolution helper ─────────────────────────────────────

    def _resolve_selection(self, session: Session, result: dict) -> None:
        """Resolve the current selection for a session.

        Updates both the inbox item (if present) and the legacy
        session.selection + session.selection_event for backward compat.
        After resolution, updates the inbox list to reflect the change
        and kicks the drain loop so the next queued item presents immediately.
        """
        # Resolve inbox item first
        item = getattr(session, '_active_inbox_item', None)
        if item and not item.done:
            item.result = result
            item.done = True
            item.event.set()

        # Legacy path (backward compat)
        session.selection = result
        session.selection_event.set()

        # Kick the drain loop so the next queued item wakes immediately
        session.drain_kick.set()

        # Update inbox list to show item as done
        self._safe_call(self._update_inbox_list)

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
        except RuntimeError as exc:
            if "App is not running" in str(exc):
                # TUI is restarting — signal the tool dispatch to retry
                return {"selected": "_restart", "summary": "TUI restarting"}
            raise
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
            # Notify via webhook
            try:
                self._notifier.notify(NotificationEvent(
                    event_type="error",
                    title="TUI Error",
                    message=f"Choice presentation error: {err}",
                    priority=4,
                    tags=["x", "error"],
                ))
            except Exception:
                pass
            # Return error to agent so it has context
            return {"selected": "error", "summary": f"TUI error: {err}"}

    def _present_choices_inner(self, session: Session, preamble: str, choices: list[dict]) -> dict:
        """Inner implementation of present_choices.

        Uses the inbox queue drain pattern: each call creates an InboxItem
        and enqueues it. If we're at the front of the queue, we present
        choices immediately. If not, we wait until it's our turn.

        Duplicate detection: if an identical item is already pending,
        we piggyback on it — wait for its event and return its result.
        This prevents MCP client retries from spamming the inbox.
        """
        import time as _time

        self._touch_session(session)

        # Create and atomically dedup+enqueue our inbox item.
        # dedup_and_enqueue() returns:
        #   True — item was enqueued as new
        #   InboxItem — existing pending item to piggyback on
        item = InboxItem(kind="choices", preamble=preamble, choices=list(choices))
        enqueued = session.dedup_and_enqueue(item)

        if isinstance(enqueued, InboxItem):
            # Piggyback on existing pending item — wait for its result.
            # This is an MCP retry; the original is already queued/presented.
            existing = enqueued
            existing.event.wait()
            return existing.result or {"selected": "_restart", "summary": "Piggyback resolved"}

        if not enqueued:
            # Item was suppressed as a duplicate — return the pre-set result
            return item.result or {"selected": "_restart", "summary": "Duplicate suppressed"}

        # Update tab bar to show inbox count
        self._safe_call(self._update_tab_bar)

        # Play inbox chime if user is already viewing choices for this session
        if session.active and self._is_focused(session.session_id):
            self._tts.play_chime("inbox")
            self._safe_call(self._update_inbox_list)

        # Kick a drain worker in case there are speech items ahead of us
        self._drain_session_inbox_worker(session)

        # ── Drain loop: wait for our turn, then present ──
        while True:
            front = session.peek_inbox()

            if front is item:
                # We're at the front — present our choices
                result = self._activate_and_present(session, item)

                # Drain completed item
                session.peek_inbox()  # moves done items to inbox_done

                # Wake up the next queued item (if any) immediately
                session.drain_kick.set()
                self._safe_call(self._update_tab_bar)

                return result

            # Not at front — wait for our turn via drain_kick or item event
            session.drain_kick.clear()
            # Check if we were resolved while checking the queue
            if item.done:
                return item.result or {"selected": "timeout", "summary": ""}
            session.drain_kick.wait(timeout=0.5)
            if item.done:
                # We were resolved externally (e.g. quit, restart)
                return item.result or {"selected": "timeout", "summary": ""}

    def _activate_and_present(self, session: Session, item: InboxItem) -> dict:
        """Activate an inbox item as the current choice presentation.

        Sets up session state from the item and blocks until the user selects.
        """
        import time as _time

        preamble = item.preamble
        choices = item.choices

        session.preamble = preamble
        session.choices = list(choices)
        session.selection = None
        session.selection_event.clear()
        session.active = True
        session.intro_speaking = True
        session.reading_options = False
        session.in_settings = False
        session._active_inbox_item = item  # Track for _do_select resolution
        self._last_spoken_text = ""  # Reset dedup for new choices

        # Force-exit ALL modals/menus if this is the focused session —
        # incoming choices take priority over settings, dialogs, etc.
        is_fg = self._is_focused(session.session_id)
        if is_fg and self._in_settings:
            self._clear_all_modal_state(session=session)
            # Guard: prevent any pending Enter/selection from leaking into
            # the freshly-presented choices (same guard as _exit_settings).
            self._settings_just_closed = True
            # Use call_from_thread since we're on the tool dispatch thread,
            # not the Textual event loop.
            try:
                self.call_from_thread(lambda: self.set_timer(0.3, self._clear_settings_guard))
            except RuntimeError:
                # If app is not running, just clear the guard directly
                self._settings_just_closed = False

        # Emit event for remote frontends
        try:
            frontend_api.emit_choices_presented(session.session_id, preamble, choices)
        except Exception:
            pass

        # ── Conversation mode: speak preamble then auto-record ──
        if self._conversation_mode and is_fg:
            session.intro_speaking = False
            session.reading_options = False

            # Audio cue
            self._tts.play_chime("choices")

            # Show conversation UI
            def _show_convo():
                self.query_one("#main-content").display = False
                self.query_one("#dwell-bar").display = False
                preamble_widget = self.query_one("#preamble", Label)
                preamble_widget.update(f"[bold {self._cs['success']}]🗣[/bold {self._cs['success']}] {preamble}")
                preamble_widget.display = True
                status = self.query_one("#status", Label)
                status.update(f"[dim]Conversation mode[/dim] [{self._cs['blue']}](c to exit)[/{self._cs['blue']}]")
                status.display = True
            self._safe_call(_show_convo)

            # Speak preamble only (no options readout)
            self._fg_speaking = True
            self._tts.speak(preamble)
            self._fg_speaking = False

            # Auto-start voice recording after a brief pause
            _time.sleep(0.3)

            # Check if conversation mode is still active (user might have pressed c)
            if self._conversation_mode and session.active:
                self._safe_call(self._start_voice_recording)

            # Block until selection (voice recording will set it)
            item.event.wait()
            session.active = False

            # Reset ambient timer
            session.last_tool_call = _time.time()
            session.ambient_count = 0

            self._safe_call(self._update_tab_bar)
            return item.result or session.selection or {"selected": "timeout", "summary": ""}

        # ── Normal mode: full choice presentation ──
        # Build the full list: extras + real choices
        # Reset extras to collapsed for each new choice presentation
        self._extras_expanded = False
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

        # Show UI immediately if this is the focused session
        if is_fg:
            self._safe_call(self._show_choices)

        # Update tab bar (session now has active choices indicator)
        self._safe_call(self._update_tab_bar)

        # Pregenerate per-option clips in background
        bg_texts = (
            numbered_full_all
            + [f"Selected: {c.get('label', '')}" for c in choices]
            + [f"{e['label']}. {e['summary']}" for e in EXTRA_OPTIONS if e.get('summary')]
        )
        self._pregenerate_worker(bg_texts)

        if is_fg:
            # Wait for any in-progress speak_async to finish before
            # starting the intro readout (which would kill it via stop).
            self._tts.wait_for_speech(timeout=5.0)

            # Audio + haptic cue for new choices
            self._tts.play_chime("choices")
            self._vibrate_pattern("pulse")

            # Foreground: speak intro and read options
            self._fg_speaking = True
            self._tts.speak(full_intro)

            session.intro_speaking = False
            # Only read options if intro wasn't interrupted by scrolling
            if session.active and not session.selection:
                session.reading_options = True
                for i, text in enumerate(numbered_full_all):
                    # Check session still exists and is active on each iteration
                    if not session.reading_options or not session.active:
                        break
                    if not self.manager.get(session.session_id):
                        break  # session was removed
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
            # Use distinct "inbox" chime if user is already viewing choices from
            # another session, to signal "new mail" without confusion
            focused = self._focused()
            if focused and focused.active and focused.session_id != session.session_id:
                self._tts.play_chime("inbox")
            else:
                self._tts.play_chime("choices")
            self._speak_ui(f"{session.name} has choices")

            # Try to speak in background if fg is idle
            self._try_play_background_queue()

        # Block until selection (on the inbox item's event, not session.selection_event)
        item.event.wait()
        session.active = False

        # Reset ambient timer — selection counts as activity
        session.last_tool_call = _time.time()
        session.ambient_count = 0

        self._safe_call(self._update_tab_bar)

        return item.result or session.selection or {"selected": "timeout", "summary": ""}

    def present_multi_select(self, session: Session, preamble: str, choices: list[dict]) -> list[dict]:
        """Show choices with toggleable checkboxes. Returns list of selected items.

        Uses the same UI as present_choices but:
        - Labels are prefixed with [ ] or [x] to show checked state
        - Enter toggles the current item instead of selecting
        - A "Done" item at the end submits all checked items
        """
        self._touch_session(session)
        checked = [False] * len(choices)

        # Add "Done" as the last choice
        done_label = ">> Done -- submit selections"
        augmented = list(choices) + [{"label": done_label, "summary": "Submit all checked items"}]

        def _make_labels():
            """Build choice labels with checkbox state."""
            result = []
            for i, c in enumerate(choices):
                prefix = "x" if checked[i] else " "
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
                check_label = f"[x] {c.get('label', '')}"
                uncheck_label = f"[ ] {c.get('label', '')}"
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
                    # Extra option — use the widget's label directly
                    text = item.choice_label
                    if item.choice_summary:
                        text = f"{text}. {item.choice_summary}"
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
            # Play inbox chime (distinct from choices chime) since user is busy
            self._tts.play_chime("inbox")
            return

        preamble_widget = self.query_one("#preamble", Label)
        preamble_widget.update(session.preamble)
        preamble_widget.display = True

        self.query_one("#status").display = False

        # Show the main content container (with inbox list if applicable)
        self._ensure_main_content_visible(show_inbox=True)

        list_view = self.query_one("#choices", ListView)
        list_view.clear()

        # Build the extras portion based on expand/collapse state
        if self._extras_expanded:
            # Expanded: show all extras (secondary + primary)
            visible_extras = list(SECONDARY_EXTRAS) + list(PRIMARY_EXTRAS)
        else:
            # Collapsed: just "More options ›" + primary extras (Record response)
            visible_extras = [MORE_OPTIONS_ITEM] + list(PRIMARY_EXTRAS)

        # Add extras (negative indices)
        for i, e in enumerate(visible_extras):
            logical_idx = -(len(visible_extras) - 1 - i)
            list_view.append(ChoiceItem(
                e["label"], e.get("summary", ""),
                index=logical_idx, display_index=i,
            ))

        # Add real choices (indices 1, 2, 3, ...)
        for i, c in enumerate(session.choices):
            list_view.append(ChoiceItem(
                c.get("label", "???"), c.get("summary", ""),
                index=i + 1, display_index=len(visible_extras) + i,
            ))

        list_view.display = True
        # Restore scroll position or default to first real choice
        n_extras = len(visible_extras)
        if session.scroll_index > 0 and session.scroll_index < len(list_view.children):
            list_view.index = session.scroll_index
        elif len(list_view.children) > n_extras:
            list_view.index = n_extras  # first real choice
        else:
            list_view.index = 0

        # Focus the appropriate pane — respect restored inbox focus state
        # (e.g. when switching back to a session that had inbox focused)
        if self._inbox_pane_focused and self._inbox_pane_visible():
            try:
                inbox_list = self.query_one("#inbox-list", ListView)
                inbox_list.focus()
            except Exception:
                list_view.focus()
        else:
            self._inbox_pane_focused = False
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
        """Show waiting state after selection, staying in inbox view.

        After resolving a choice, the inbox view persists showing the
        done item. If there are more pending items, the next one will
        auto-present via the inbox drain loop.
        """
        self.query_one("#preamble").display = False
        self.query_one("#dwell-bar").display = False
        status = self.query_one("#status", Label)
        session = self._focused()
        session_name = session.name if session else ""
        after_text = f"Selected: {label}" if self._demo else f"[{self._cs['success']}]*[/{self._cs['success']}] [{session_name}] {label} [dim](u=undo)[/dim]"
        status.update(after_text)
        status.display = True

        # Stay in inbox view — update inbox list to reflect the done item
        self._ensure_main_content_visible(show_inbox=True)
        self._inbox_pane_focused = False

        # Show a simple waiting state in the right pane (not full activity feed)
        list_view = self.query_one("#choices", ListView)
        list_view.clear()
        s = self._cs
        list_view.append(ChoiceItem(
            f"[{s['fg_dim']}]Waiting for agent...[/{s['fg_dim']}]", "",
            index=-999, display_index=0,
        ))
        list_view.display = True

    def _show_idle(self) -> None:
        """Show idle state with inbox view."""
        self.query_one("#preamble").display = False
        self.query_one("#dwell-bar").display = False
        self.query_one("#speech-log").display = False
        status = self.query_one("#status", Label)
        session = self._focused()

        if session is None:
            status_text = "[dim]Ready -- demo mode[/dim]" if self._demo else "[dim]Waiting for agent...[/dim]"
            status.update(status_text)
            status.display = True
            self.query_one("#main-content").display = False
            return

        if session.tool_call_count > 0:
            status_text = f"[dim]{session.name} -- working...[/dim]"
        else:
            status_text = f"[{self._cs['accent']}]{session.name} connected[/{self._cs['accent']}]"
        status.update(status_text)
        status.display = True

        # Show inbox view with history
        self._ensure_main_content_visible(show_inbox=True)

        # Show simple waiting state in right pane
        s = self._cs
        list_view = self.query_one("#choices", ListView)
        list_view.clear()
        list_view.append(ChoiceItem(
            f"[{s['fg_dim']}]Waiting for agent...[/{s['fg_dim']}]", "",
            index=-999, display_index=0,
        ))
        list_view.display = True

    def _show_waiting_with_shortcuts(self, session) -> None:
        """Show a clean waiting state with essential keyboard shortcuts.

        Replaces the old activity feed. Shows the inbox view (left pane)
        with a minimal right pane containing status and shortcut hints.
        """
        try:
            s = self._cs

            # Show inbox view with history
            self._ensure_main_content_visible(show_inbox=True)

            list_view = self.query_one("#choices", ListView)
            list_view.clear()

            if session is None:
                list_view.display = False
                return

            di = 0  # display_index counter

            # ── Status line ──────────────────────────────────
            if session.tool_call_count > 0:
                import time as _time
                elapsed = _time.time() - session.last_tool_call
                if elapsed < 60:
                    ago = f"{int(elapsed)}s ago"
                elif elapsed < 3600:
                    ago = f"{int(elapsed) // 60}m ago"
                else:
                    ago = f"{int(elapsed) // 3600}h ago"
                status_text = f"[{s['fg_dim']}]Agent working... last activity {ago}[/{s['fg_dim']}]"
            else:
                status_text = f"[{s['fg_dim']}]Waiting for agent...[/{s['fg_dim']}]"
            list_view.append(ChoiceItem(
                status_text, "",
                index=-999, display_index=di,
            ))
            di += 1

            # ── Essential shortcuts ──────────────────────────
            shortcuts = [
                ("m", "Queue message"),
                ("s", "Settings"),
                ("d", "Dashboard"),
            ]
            if session.tmux_pane:
                shortcuts.insert(2, ("v", "Pane view"))

            shortcut_parts = [f"[{s['fg_dim']}]{key}[/{s['fg_dim']}]={label}" for key, label in shortcuts]
            list_view.append(ChoiceItem(
                f"[{s['fg_dim']}]  {' · '.join(shortcut_parts)}[/{s['fg_dim']}]", "",
                index=-998, display_index=di,
            ))
            di += 1

            # ── Pending messages indicator ───────────────────
            if session.pending_messages:
                count = len(session.pending_messages)
                list_view.append(ChoiceItem(
                    f"[{s['purple']}]{count} message{'s' if count != 1 else ''} queued[/{s['purple']}]",
                    f"[{s['fg_dim']}]{session.pending_messages[-1][:60]}[/{s['fg_dim']}]",
                    index=-994, display_index=di,
                ))
                di += 1

            list_view.display = True
            list_view.index = 0
        except Exception:
            # Guard against app not running or widget errors during shutdown
            pass

    # ─── Inbox list (left pane of two-column layout) ───────────────

    @staticmethod
    def _dedup_done_items(done: list) -> list:
        """Deduplicate done inbox items by preamble.

        When agents repeatedly present choices with the same preamble
        (e.g. "What would you like to do next?"), the done list accumulates
        many visually identical entries.  This keeps only the most recent
        item for each unique preamble, preserving chronological order.
        """
        seen: dict[str, int] = {}  # preamble → index of kept item
        deduped: list = []
        for item in done:
            key = item.preamble
            if key in seen:
                # Replace the earlier item with this newer one
                deduped[seen[key]] = item
            else:
                seen[key] = len(deduped)
                deduped.append(item)
        # Remove any None placeholders (shouldn't happen, but be safe)
        return [i for i in deduped if i is not None]

    def _update_inbox_list(self) -> None:
        """Update the inbox list (left pane) with items from ALL sessions.

        Shows a unified inbox across all agents. Each item shows the agent
        name prefix so the user knows which agent sent it. Items are sorted
        by timestamp (newest first for pending, oldest first for done).

        Uses a combined generation counter to skip no-op rebuilds.
        Skips UI updates when user is composing freeform input or a message.
        """
        # Don't rebuild inbox while user is typing — it can steal focus
        if self._message_mode or self._filter_mode:
            return
        session = self._focused()
        if session and getattr(session, 'input_mode', False):
            return

        try:
            inbox_list = self.query_one("#inbox-list", ListView)
            sessions = self.manager.all_sessions()

            if not sessions:
                inbox_list.clear()
                inbox_list.display = False
                self._inbox_last_generation = -1
                return

            # Combined generation counter across all sessions
            gen = sum(s._inbox_generation for s in sessions)
            if gen == self._inbox_last_generation:
                if self._inbox_scroll_index < len(inbox_list.children):
                    inbox_list.index = self._inbox_scroll_index
                return
            self._inbox_last_generation = gen

            inbox_list.clear()

            # Collect items from all sessions
            all_pending: list[tuple[InboxItem, Session]] = []
            all_done: list[tuple[InboxItem, Session]] = []
            any_registered = False

            for sess in sessions:
                if sess.registered:
                    any_registered = True
                for item in sess.inbox:
                    if not item.done:
                        all_pending.append((item, sess))
                for item in sess.inbox_done:
                    all_done.append((item, sess))

            # Deduplicate done items per session
            done_deduped: list[tuple[InboxItem, Session]] = []
            for sess in sessions:
                sess_done = [item for item in sess.inbox_done]
                deduped = self._dedup_done_items(sess_done)
                for item in deduped[-5:]:  # Last 5 done per session
                    done_deduped.append((item, sess))

            total = len(all_pending) + len(done_deduped)

            if total == 0 and not any_registered:
                inbox_list.display = False
                return

            inbox_list.display = True

            s = getattr(self, '_cs', {})
            accent_color = s.get('accent', '#88c0d0')
            multi_agent = self.manager.count() > 1

            # Determine active items across all sessions
            active_items = set()
            for sess in sessions:
                ai = getattr(sess, '_active_inbox_item', None)
                if ai is not None:
                    active_items.add(id(ai))

            # Sort pending by timestamp (newest first)
            all_pending.sort(key=lambda x: x[0].timestamp, reverse=True)
            # Sort done by timestamp (newest first)
            done_deduped.sort(key=lambda x: x[0].timestamp, reverse=True)

            idx = 0
            for item, sess in all_pending:
                is_active = id(item) in active_items
                inbox_list.append(InboxListItem(
                    preamble=item.preamble or item.text,
                    is_done=False,
                    is_active=is_active,
                    inbox_index=idx,
                    n_choices=len(item.choices),
                    session_name=sess.name if multi_agent else "",
                    accent_color=accent_color if multi_agent else "",
                    kind=item.kind,
                    session_id=sess.session_id,
                ))
                idx += 1

            for item, sess in done_deduped[:10]:  # Show last 10 done total
                inbox_list.append(InboxListItem(
                    preamble=item.preamble or item.text,
                    is_done=True,
                    is_active=False,
                    inbox_index=idx,
                    n_choices=len(item.choices),
                    session_name=sess.name if multi_agent else "",
                    accent_color=accent_color if multi_agent else "",
                    kind=item.kind,
                    session_id=sess.session_id,
                ))
                idx += 1

            # Highlight the active item
            if self._inbox_scroll_index < len(inbox_list.children):
                inbox_list.index = self._inbox_scroll_index
            else:
                inbox_list.index = 0

        except Exception:
            pass

    def _get_inbox_item_at_index(self, idx: int):
        """Get the InboxItem corresponding to an inbox list position.

        Mirrors the unified inbox ordering from _update_inbox_list:
        all pending items sorted by timestamp desc, then done items.
        Returns the InboxItem or None.
        """
        sessions = self.manager.all_sessions()
        if not sessions:
            return None

        # Collect from all sessions (same logic as _update_inbox_list)
        all_pending: list[InboxItem] = []
        all_done: list[InboxItem] = []

        for sess in sessions:
            for item in sess.inbox:
                if not item.done:
                    all_pending.append(item)
            sess_done = self._dedup_done_items(list(sess.inbox_done))
            all_done.extend(sess_done[-5:])

        all_pending.sort(key=lambda x: x.timestamp, reverse=True)
        all_done.sort(key=lambda x: x.timestamp, reverse=True)

        ordered = all_pending + all_done[:10]

        if 0 <= idx < len(ordered):
            return ordered[idx]
        return None

    def _handle_inbox_select(self, inbox_widget: InboxListItem) -> None:
        """Handle selection of an item in the inbox list (left pane).

        If the item is pending, switch it to be the active item and show
        its choices in the right pane.
        If the item is done, show its resolved result as read-only info.
        Speech items show the speech text; choice items show the selection.
        """
        session = self._focused()
        if not session:
            return

        idx = inbox_widget.inbox_index
        inbox_item = self._get_inbox_item_at_index(idx)
        if inbox_item is None:
            return

        if inbox_item.done:
            s = self._cs

            # Speech items: show the speech text
            if inbox_item.kind == "speech":
                text = inbox_item.text or inbox_item.preamble
                preamble_widget = self.query_one("#preamble", Label)
                preamble_widget.update(f"[dim][{s['blue']}]♪[/{s['blue']}] {text}[/dim]")
                preamble_widget.display = True
                self.query_one("#status").display = False

                list_view = self.query_one("#choices", ListView)
                list_view.clear()
                list_view.append(ChoiceItem(
                    f"[{s['fg_dim']}]Speech played[/{s['fg_dim']}]", "",
                    index=-999, display_index=0,
                ))
                list_view.display = True
                list_view.index = 0
                self._inbox_pane_focused = False
                list_view.focus()
                self._inbox_scroll_index = idx
                self._update_inbox_list()
                self._speak_ui(text)
                return

            # Choice items: show the resolved result
            result = inbox_item.result or {}
            label = result.get("selected", "(no selection)")
            summary = result.get("summary", "")

            # Update right pane to show the resolved item's details
            s = self._cs
            preamble_widget = self.query_one("#preamble", Label)
            preamble_widget.update(f"[dim]{inbox_item.preamble}[/dim]")
            preamble_widget.display = True
            self.query_one("#status").display = False

            list_view = self.query_one("#choices", ListView)
            list_view.clear()
            list_view.append(ChoiceItem(
                f"[{s['success']}]✓ {label}[/{s['success']}]",
                summary if summary else "",
                index=-999, display_index=0,
            ))
            # Show original choices as dimmed reference
            for i, c in enumerate(inbox_item.choices):
                choice_label = c.get("label", "")
                is_selected = choice_label == label
                if is_selected:
                    list_view.append(ChoiceItem(
                        f"  [{s['success']}]» {choice_label}[/{s['success']}]",
                        c.get("summary", ""),
                        index=-998 + i, display_index=i + 1,
                    ))
                else:
                    list_view.append(ChoiceItem(
                        f"  [{s['fg_dim']}]{choice_label}[/{s['fg_dim']}]",
                        f"[{s['fg_dim']}]{c.get('summary', '')}[/{s['fg_dim']}]",
                        index=-998 + i, display_index=i + 1,
                    ))
            list_view.display = True
            list_view.index = 0

            # Focus the right pane (choices) so keyboard navigation works
            self._inbox_pane_focused = False
            list_view.focus()

            # Update inbox list to highlight the selected done item
            self._inbox_scroll_index = idx
            self._update_inbox_list()

            self._speak_ui(f"Resolved: {label}")
            return

        # Make this the active inbox item and show its choices
        self._tts.stop()
        session.preamble = inbox_item.preamble
        session.choices = list(inbox_item.choices)
        session.selection = None
        session.selection_event.clear()
        session.active = True
        session._active_inbox_item = inbox_item
        self._extras_expanded = False
        session.extras_count = len(EXTRA_OPTIONS)
        session.all_items = list(EXTRA_OPTIONS) + session.choices

        # Switch focus to right pane
        self._inbox_pane_focused = False
        self._show_choices()

        # Speak the preamble
        self._tts.speak_async(inbox_item.preamble)

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
            self._safe_call(self._update_speech_log)

        if self._is_focused(session.session_id):
            # Foreground: play immediately
            voice_ov = getattr(session, 'voice_override', None)
            model_ov = getattr(session, 'model_override', None)
            # Per-call emotion > session override > config default
            emotion_ov = emotion if emotion else getattr(session, 'emotion_override', None)

            # Urgent messages always interrupt
            if priority >= 1:
                self._tts.stop()

            self._fg_speaking = True
            if block:
                # Use cached play for blocking calls to avoid
                # streaming truncation (audio starting mid-sentence)
                self._tts.speak(text, voice_override=voice_ov,
                                emotion_override=emotion_ov,
                                model_override=model_ov)
            else:
                self._tts.speak_async(text, voice_override=voice_ov,
                                     emotion_override=emotion_ov,
                                     model_override=model_ov)
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
        """Legacy blocking TTS — plays directly, does NOT create inbox items."""
        self._tts.speak(text)

    def speak_async(self, text: str) -> None:
        """Legacy non-blocking TTS — plays directly, does NOT create inbox items."""
        self._tts.speak_async(text)

    # ─── Inbox drain (speech items) ───────────────────────────────

    def notify_inbox_update(self, session: Session) -> None:
        """Called from tool dispatch when a new item is enqueued.

        Kicks the drain loop so speech items at the front of the queue
        get played immediately. Also updates the inbox UI and scrolls
        to the top to show the newest item.
        """
        self._touch_session(session)
        session.drain_kick.set()
        # Scroll inbox to top so newest item is visible
        self._inbox_scroll_index = 0
        self._safe_call(self._update_tab_bar)
        self._safe_call(self._update_inbox_list)

        # Start a drain worker for this session if speech items need processing
        self._drain_session_inbox_worker(session)

    @work(thread=True, exit_on_error=False, group="drain_inbox")
    def _drain_session_inbox_worker(self, session: Session) -> None:
        """Worker: drain speech items from session inbox in background thread."""
        self._drain_session_inbox(session)

    def _drain_session_inbox(self, session: Session) -> None:
        """Background drain loop: process speech items at the front of the inbox.

        Speech items are auto-resolved after TTS playback. Choice items are
        handled by _present_choices_inner (which runs on the tool thread).
        This method only processes speech items — it exits when it hits a
        choice item or the queue is empty.
        """
        while True:
            front = session.peek_inbox()
            if front is None:
                break
            if front.kind != "speech":
                # Choice item — handled by _present_choices_inner
                break
            if front.done:
                continue

            # ── Process speech item ──
            self._activate_speech_item(session, front)

    def _activate_speech_item(self, session: Session, item: InboxItem) -> None:
        """Play TTS for a speech inbox item and auto-resolve it.

        Shows the speech text in the inbox sidebar and right pane,
        plays TTS, then marks the item done.
        """
        text = item.text or item.preamble
        priority = item.priority

        self._touch_session(session)

        # Log the speech
        entry = SpeechEntry(text=text, priority=priority)
        session.speech_log.append(entry)

        # Emit event for remote frontends
        try:
            frontend_api.emit_speech_requested(
                session.session_id, text, blocking=True, priority=priority)
        except Exception:
            pass

        # Update inbox UI to show this item as active
        session._active_inbox_item = item
        self._safe_call(self._update_inbox_list)

        # Show speech text in right pane if this is the focused session
        if self._is_focused(session.session_id):
            self._safe_call(lambda: self._show_speech_item(text))

        # Play TTS
        voice_ov = getattr(session, 'voice_override', None)
        model_ov = getattr(session, 'model_override', None)
        emotion_ov = getattr(session, 'emotion_override', None)

        if priority >= 1:
            self._tts.stop()

        self._tts.speak(text, voice_override=voice_ov,
                        emotion_override=emotion_ov,
                        model_override=model_ov)

        # Auto-resolve: mark done and kick drain
        item.result = {"selected": "_speech_done", "summary": text[:100]}
        item.done = True
        item.event.set()
        session._append_done(session.inbox.popleft())
        session.drain_kick.set()
        self._safe_call(self._update_inbox_list)
        self._safe_call(self._update_tab_bar)

    def _show_speech_item(self, text: str) -> None:
        """Show a speech item's text in the right pane (runs on textual thread)."""
        # Don't update the main screen UI when a modal (text input) is open
        if isinstance(self.screen, TextInputModal):
            return
        try:
            s = self._cs
            preamble_widget = self.query_one("#preamble", Label)
            # Show the speech text with a music note icon
            preamble_widget.update(f"[{s['blue']}]♪[/{s['blue']}] [{s['fg']}]{text}[/{s['fg']}]")
            preamble_widget.display = True
            self.query_one("#status").display = False
            self._ensure_main_content_visible(show_inbox=True)

            # Show a simple "playing" indicator in the choices area
            list_view = self.query_one("#choices", ListView)
            list_view.clear()
            list_view.display = True
        except Exception:
            pass

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
        # Save current scroll position and inbox pane focus state
        old_session = self._focused()
        if old_session and old_session.session_id != session.session_id:
            try:
                list_view = self.query_one("#choices", ListView)
                old_session.scroll_index = list_view.index or 0
            except Exception:
                pass
            old_session.inbox_pane_focused = self._inbox_pane_focused
            # Clear per-session settings state on the old session so it
            # doesn't linger when switching back later.
            old_session.in_settings = False
            old_session.reading_options = False

        # Stop current TTS
        self._tts.stop()
        if old_session:
            old_session.reading_options = False

        # Focus new session — restore its saved inbox pane focus state
        self.manager.focus(session.session_id)
        self._inbox_pane_focused = session.inbox_pane_focused
        self._inbox_last_generation = -1  # force inbox rebuild for new session

        # Update UI directly (we're on the main thread)
        self._update_tab_bar()

        if session.active:
            # Session has active choices — show them
            self._show_choices()

            # Play back unplayed speech then read prompt+options via worker
            self._play_inbox_and_read_worker(session)
        else:
            # No active choices — show idle state with activity feed
            self._show_idle()

            # Play unplayed speech via worker
            if session.unplayed_speech:
                self._play_inbox_only_worker(session)

            self._show_session_waiting(session)

    @work(thread=True, exit_on_error=False, group="play_inbox")
    def _play_inbox_and_read_worker(self, session: Session) -> None:
        """Worker: play unplayed speech then read prompt+options in background."""
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

    @work(thread=True, exit_on_error=False, group="play_inbox")
    def _play_inbox_only_worker(self, session: Session) -> None:
        """Worker: play unplayed speech entries in background."""
        while session.unplayed_speech:
            entry = session.unplayed_speech.pop(0)
            entry.played = True
            self._fg_speaking = True
            self._tts.speak(entry.text)
            self._fg_speaking = False

    def _show_session_waiting(self, session: Session) -> None:
        """Show waiting state for a specific session."""
        # Don't overwrite UI when user is typing a message or freeform input
        if self._message_mode or getattr(session, 'input_mode', False):
            return
        self.query_one("#preamble").display = False
        self.query_one("#dwell-bar").display = False
        self._update_speech_log()
        status = self.query_one("#status", Label)
        # Show pending message count if any
        msgs = getattr(session, 'pending_messages', [])
        msg_info = f" [dim]·[/dim] [{self._cs['purple']}]{len(msgs)} msg{'s' if len(msgs) != 1 else ''}[/{self._cs['purple']}]" if msgs else ""
        status.update(f"[{self._cs['warning']}]⧗[/{self._cs['warning']}] [{session.name}] Waiting for agent...{msg_info} [dim](u=undo)[/dim]")
        status.display = True

        # Clear the right pane and show a clean waiting state
        # (prevents stale extras/activity feed from persisting)
        s = self._cs
        list_view = self.query_one("#choices", ListView)
        list_view.clear()
        list_view.append(ChoiceItem(
            f"[{s['fg_dim']}]Waiting for agent...[/{s['fg_dim']}]", "",
            index=-999, display_index=0,
        ))
        list_view.display = True

    # ─── Dialog system ────────────────────────────────────────────

    def _show_dialog(
        self,
        title: str,
        message: str,
        buttons: list[dict],
        callback: callable,
        speak_title: bool = True,
    ) -> None:
        """Show a modal dialog with action buttons.

        Uses the choice list UI but styled as a dialog with a title,
        message body, and action buttons. The callback receives the
        selected button's label.

        Args:
            title: Dialog title (shown in preamble area)
            message: Body text (shown as a dim info item at top)
            buttons: List of {"label": "...", "summary": "..."} dicts
            callback: Function called with selected label string
            speak_title: Whether to speak the title via TTS
        """
        session = self._focused()

        self._tts.stop()

        # Enter a settings-like modal
        self._in_settings = True
        self._dialog_callback = callback
        self._dialog_buttons = buttons

        s = self._cs
        preamble_widget = self.query_one("#preamble", Label)
        preamble_widget.update(f"[bold {s['purple']}]{title}[/bold {s['purple']}]")
        preamble_widget.display = True

        # Ensure main content is visible, hide inbox pane in dialog
        self._ensure_main_content_visible(show_inbox=False)

        list_view = self.query_one("#choices", ListView)
        list_view.clear()

        # Message body as a non-selectable info item
        if message:
            list_view.append(ChoiceItem(
                f"[dim]{message}[/dim]", "",
                index=0, display_index=0,
            ))

        # Action buttons
        for i, btn in enumerate(buttons):
            display_idx = i + (1 if message else 0)
            list_view.append(ChoiceItem(
                btn["label"], btn.get("summary", ""),
                index=i + 1, display_index=display_idx,
            ))

        list_view.display = True
        # Focus first button (skip message)
        list_view.index = 1 if message else 0
        list_view.focus()

        if speak_title:
            self._tts.speak_async(f"{title}. {message}" if message else title)

    def _handle_dialog_select(self, idx: int) -> None:
        """Handle a selection in dialog mode."""
        buttons = getattr(self, '_dialog_buttons', [])
        callback = getattr(self, '_dialog_callback', None)

        # Adjust for message item offset
        btn_idx = idx - 1 if hasattr(self, '_dialog_has_message') else idx
        # Actually check if first item is a message
        list_view = self.query_one("#choices", ListView)
        first_item = list_view.children[0] if list_view.children else None
        has_message = first_item and isinstance(first_item, ChoiceItem) and first_item.choice_index == 0

        if has_message:
            btn_idx = idx - 1
        else:
            btn_idx = idx

        if btn_idx < 0:
            # Clicked on message — ignore
            return

        # Exit dialog
        session = self._focused()
        self._clear_all_modal_state(session=session)

        if btn_idx < len(buttons) and callback:
            label = buttons[btn_idx]["label"]
            self._tts.speak_async(label)
            callback(label)
        else:
            # Cancel / out of bounds
            self._exit_settings()

    def _request_compact(self) -> None:
        """Request context compaction by telling the agent to run /compact.

        If the agent has active choices, resolves them with a compact
        instruction. Otherwise, queues a message for the agent's next
        tool response.
        """
        session = self._focused()
        if not session:
            self._speak_ui("No active session to compact")
            return

        self._tts.play_chime("select")

        compact_msg = (
            "COMPACT YOUR CONTEXT NOW. Send '/compact' then Enter to your own "
            "tmux pane, then send Enter again to confirm the menu selection. "
            "Use: tmux send-keys -t $TMUX_PANE '/compact' Enter && sleep 2 && "
            "tmux send-keys -t $TMUX_PANE Enter. "
            "Wait 60 seconds for compaction to complete, then continue working."
        )

        if session.active:
            # Resolve active choices with the compact instruction
            self._resolve_selection(session, {"selected": compact_msg, "summary": "(compact context)"})
            self._show_waiting("Compact context")
            self._speak_ui("Compact command sent to agent")
        else:
            # Queue as a pending message for next tool response
            msgs = getattr(session, 'pending_messages', None)
            if msgs is not None:
                msgs.append(compact_msg)
            self._speak_ui("Compact request queued for agent")

    def _restart_tui(self) -> None:
        """Restart the TUI by exiting with a special code that triggers the restart loop.

        The main() function wraps app.run() in a restart loop. Exit code 42
        means "restart", any other code means "quit for real".
        """
        def _on_confirm(label: str):
            if label.lower().startswith("restart"):
                self._speak_ui("Restarting TUI in 2 seconds")
                self._do_tui_restart_worker()
            else:
                self._exit_settings()

        self._show_dialog(
            title="Restart TUI?",
            message="The TUI will restart. Agent connections via proxy are preserved.",
            buttons=[
                {"label": "Restart now", "summary": "Restart the TUI backend process"},
                {"label": "Cancel", "summary": "Go back to choices"},
            ],
            callback=_on_confirm,
        )

    @work(thread=True, exit_on_error=False, name="tui_restart")
    def _do_tui_restart_worker(self) -> None:
        """Worker: delayed TUI restart in background thread."""
        time.sleep(2.0)
        # Unblock all pending selection waits so backend threads
        # don't hang forever after the app is replaced
        for sess in self.manager.all_sessions():
            if sess.active:
                self._resolve_selection(sess, {"selected": "_restart", "summary": "TUI restarting"})
        self._restart_requested = True
        self.exit(return_code=42)

    def _restart_proxy_from_tui(self) -> None:
        """Restart the MCP proxy from the TUI.

        Kills the proxy, restarts it, and reports the result via TTS.
        Agents will need to reconnect.
        """
        def _on_confirm(label: str):
            if label.lower().startswith("restart"):
                self._speak_ui("Restarting MCP proxy")
                self._do_proxy_restart_worker()
            else:
                self._exit_settings()

        self._show_dialog(
            title="Restart MCP Proxy?",
            message="All agent MCP connections will drop. They must reconnect.",
            buttons=[
                {"label": "Restart proxy", "summary": "Kill and restart the MCP proxy"},
                {"label": "Cancel", "summary": "Go back"},
            ],
            callback=_on_confirm,
        )

    @work(thread=True, exit_on_error=False, name="proxy_restart")
    def _do_proxy_restart_worker(self) -> None:
        """Worker: restart MCP proxy in background thread."""
        from . import __main__ as main_mod
        dev_mode = "--dev" in sys.argv
        success = main_mod._restart_proxy(dev=dev_mode)
        if success:
            self._tts.speak_async("Proxy restarted. Agents need to reconnect.")
        else:
            self._tts.speak_async("Proxy restart failed.")
        self.call_from_thread(self._update_tab_bar)

    def _enter_worktree_mode(self) -> None:
        """Start worktree creation flow.

        Shows options: create worktree (prompts for branch name),
        or fork agent to worktree (spawns new agent in the worktree).
        """
        session = self._focused()
        if not session:
            self._speak_ui("No active session")
            return

        self._tts.stop()

        # Enter settings-like modal for worktree options
        self._in_settings = True
        self._setting_edit_mode = False
        self._spawn_options = None
        self._quick_action_options = None
        self._system_logs_mode = False
        self._help_mode = False
        self._history_mode = False
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

        self._ensure_main_content_visible(show_inbox=False)

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
        session = self._focused()
        self._clear_all_modal_state(session=session)

        if not session:
            return

        if action == "cancel":
            self._exit_settings()
            return

        if action == "branch_here":
            # Ask for branch name via modal input
            self._tts.speak_async("Type the branch name")

            def _on_branch_name(result: str | None) -> None:
                if result:
                    self._create_worktree(session, result, "branch_here")
                else:
                    self._speak_ui("Cancelled.")
                    if session.active:
                        self._show_choices()

            self.push_screen(
                TextInputModal(
                    title="Branch name",
                    message_mode=False,
                    scheme=self._cs,
                ),
                callback=_on_branch_name,
            )

        elif action == "fork_agent":
            self._tts.speak_async("Type branch name for the new agent's worktree")

            def _on_fork_branch_name(result: str | None) -> None:
                if result:
                    self._create_worktree(session, result, "fork_agent")
                else:
                    self._speak_ui("Cancelled.")
                    if session.active:
                        self._show_choices()

            self.push_screen(
                TextInputModal(
                    title="Branch name for new agent",
                    message_mode=False,
                    scheme=self._cs,
                ),
                callback=_on_fork_branch_name,
            )

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
        self._create_worktree_worker(session, branch_name, action, cwd)

    @work(thread=True, exit_on_error=False, name="create_worktree")
    def _create_worktree_worker(self, session: Session, branch_name: str, action: str, cwd: str) -> None:
        """Worker: create git worktree in background thread."""
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

    @_safe_action
    def action_multi_select_toggle(self) -> None:
        """Toggle multi-select mode.

        If currently viewing normal choices → enter multi-select mode.
        If already in multi-select mode → confirm selection (like pressing Confirm).
        """
        session = self._focused()
        if not session:
            return

        # Guard: don't activate in settings/filter/input modes
        if session.input_mode or session.voice_recording:
            return
        if self._in_settings or self._filter_mode:
            return

        if self._multi_select_mode:
            # Already in multi-select → confirm
            self._confirm_multi_select(team=False)
        else:
            # Enter multi-select mode
            self._enter_multi_select_mode()

    def _enter_multi_select_mode(self) -> None:
        """Enter multi-select mode for the current choices.

        Re-renders the choice list with checkbox indicators. Enter toggles
        each item. Adds "Confirm" and "Confirm with team" at the bottom.
        """
        session = self._focused()
        if not session or not session.active or not session.choices:
            self._speak_ui("No choices to multi-select from")
            return

        self._tts.stop()
        self._multi_select_mode = True
        self._multi_select_checked = [False] * len(session.choices)
        self._refresh_multi_select()
        self._tts.speak_async("Multi-select mode. Enter to toggle choices. Press x to confirm, q to cancel.")

    def _refresh_multi_select(self) -> None:
        """Redraw the choice list with checkbox state."""
        session = self._focused()
        if not session:
            return

        s = self._cs
        checked_count = sum(self._multi_select_checked)
        total = len(session.choices)

        preamble_widget = self.query_one("#preamble", Label)
        preamble_widget.update(
            f"[bold {s['purple']}]Multi-select[/bold {s['purple']}] — "
            f"[{s['success']}]{checked_count}[/{s['success']}]/{total} selected "
            f"[dim](enter=toggle, scroll to confirm)[/dim]"
        )
        preamble_widget.display = True

        list_view = self.query_one("#choices", ListView)

        # Remember current position
        current_idx = list_view.index or 0

        list_view.clear()

        # Select all / Deselect all toggle at top
        all_selected = all(self._multi_select_checked) if self._multi_select_checked else False
        toggle_label = f"[{s['accent']}][ ] Deselect all[/{s['accent']}]" if all_selected else f"[{s['accent']}][*] Select all[/{s['accent']}]"
        list_view.append(ChoiceItem(
            toggle_label, f"{'Deselect' if all_selected else 'Select'} all {total} items",
            index=-99, display_index=0,
        ))

        # Choices with checkboxes
        for i, c in enumerate(session.choices):
            is_checked = i < len(self._multi_select_checked) and self._multi_select_checked[i]
            if is_checked:
                check = f"[{s['success']}][x][/{s['success']}]"
            else:
                check = f"[{s['fg_dim']}][ ][/{s['fg_dim']}]"
            num = str(i + 1)
            pad = " " * (2 - len(num))
            label = f"{pad}{num}. {check}  {c.get('label', '')}"
            summary = c.get('summary', '')
            list_view.append(ChoiceItem(label, summary, index=i + 1, display_index=i + 1))

        # Confirm options
        confirm_offset = total + 1  # +1 for the select-all row
        if checked_count > 0:
            selected_labels = [
                session.choices[i].get("label", "")
                for i in range(total)
                if i < len(self._multi_select_checked) and self._multi_select_checked[i]
            ]
            selected_summary = ", ".join(selected_labels[:3])
            if len(selected_labels) > 3:
                selected_summary += f" +{len(selected_labels) - 3} more"
        else:
            selected_summary = "Nothing selected yet"

        list_view.append(ChoiceItem(
            f"[bold {s['success']}]✅ Confirm ({checked_count})[/bold {s['success']}]",
            selected_summary,
            index=total + 1, display_index=confirm_offset,
        ))
        list_view.append(ChoiceItem(
            f"[bold {s['accent']}]🚀 Team mode ({checked_count})[/bold {s['accent']}]",
            f"Delegate {checked_count} task{'s' if checked_count != 1 else ''} to parallel sub-agents",
            index=total + 2, display_index=confirm_offset + 1,
        ))
        list_view.append(ChoiceItem(
            f"[dim]Cancel[/dim]", "Return to choices",
            index=total + 3, display_index=confirm_offset + 2,
        ))

        list_view.display = True
        # Restore position
        if current_idx < len(list_view.children):
            list_view.index = current_idx
        else:
            list_view.index = 0
        list_view.focus()

    def _handle_multi_select_enter(self, idx: int) -> None:
        """Handle Enter press in multi-select mode.

        Layout (display_index):
          0 = Select all / Deselect all
          1..num_choices = Checkable choices
          num_choices+1 = Confirm
          num_choices+2 = Team mode
          num_choices+3 = Cancel
        """
        session = self._focused()
        if not session:
            return

        num_choices = len(session.choices)
        num_checked = len(self._multi_select_checked)

        if idx == 0:
            # Select all / Deselect all toggle
            all_selected = all(self._multi_select_checked) if self._multi_select_checked else False
            new_val = not all_selected
            self._multi_select_checked = [new_val] * num_choices
            state = "all selected" if new_val else "all deselected"
            self._tts.speak_async(state)
            self._refresh_multi_select()
        elif 1 <= idx <= num_choices:
            # Toggle the choice (adjusted for select-all offset)
            choice_idx = idx - 1
            if choice_idx < num_checked:
                self._multi_select_checked[choice_idx] = not self._multi_select_checked[choice_idx]
                state = "selected" if self._multi_select_checked[choice_idx] else "unselected"
                label = session.choices[choice_idx].get("label", "")
                self._tts.speak_async(f"{label} {state}")
                self._refresh_multi_select()
        elif idx == num_choices + 1:
            # Confirm selection
            self._confirm_multi_select(team=False)
        elif idx == num_choices + 2:
            # Confirm with team
            self._confirm_multi_select(team=True)
        elif idx == num_choices + 3:
            # Cancel
            self._multi_select_mode = False
            self._multi_select_checked = []
            self._show_choices()
            self._speak_ui("Multi-select cancelled.")

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
            self._speak_ui("Nothing selected. Toggle some choices first.")
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
            self._speak_ui(f"Team mode. {len(selected)} tasks for agent team.")
        else:
            response_text = (
                f"The user selected multiple actions to do sequentially:\n"
                + "\n".join(f"- {l}" for l in labels)
            )
            self._speak_ui(f"Confirmed {len(selected)} selections.")

        # Haptic + audio
        self._vibrate(100)
        self._tts.play_chime("select")

        self._resolve_selection(session, {"selected": response_text, "summary": f"(multi-select: {combined_label[:60]})"})
        self._show_waiting(f"Multi: {combined_label[:50]}")

    def _enter_tab_picker(self) -> None:
        """Enter tab picking mode.

        Shows all sessions in a list. Scrolling highlights each tab
        (switching to it live). Enter confirms and exits.
        """
        sessions = self.manager.all_sessions()
        if len(sessions) <= 1:
            self._speak_ui("Only one tab open. Press t to spawn a new agent.")
            return

        self._tts.stop()

        # Use settings infrastructure for modal
        self._in_settings = True
        self._setting_edit_mode = False
        self._spawn_options = None
        self._quick_action_options = None
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

        self._ensure_main_content_visible(show_inbox=False)

        list_view = self.query_one("#choices", ListView)
        list_view.clear()

        current_idx = 0
        for i, sess in enumerate(sessions):
            indicator = f"[{s['success']}]o[/{s['success']}] " if sess.active else ""
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
        self._speak_ui(f"Pick a tab. {len(sessions)} tabs. Scrolling switches live.")

    def _inbox_pane_visible(self) -> bool:
        """Check if the inbox pane (left column) is currently visible."""
        try:
            return self.query_one("#inbox-list", ListView).display
        except Exception:
            return False

    def action_next_tab(self) -> None:
        """Switch to next tab, or switch to choices pane if inbox is visible."""
        session = self._focused()
        if session and (session.input_mode or session.voice_recording):
            return

        # If inbox is collapsed, expand it
        if self._inbox_collapsed:
            self._inbox_collapsed = False
            self._update_inbox_list()
            self._speak_ui("Inbox expanded")
            return

        # If inbox pane is visible and we're in the inbox, switch to choices pane
        if self._inbox_pane_visible() and self._inbox_pane_focused:
            self._inbox_pane_focused = False
            self.query_one("#choices", ListView).focus()
            self._speak_ui("Choices")
            return

        if self.manager.count() <= 1:
            return
        new_session = self.manager.next_tab()
        if new_session and new_session.session_id != (session.session_id if session else None):
            self._tts.stop()
            self._tts.speak_async(new_session.name)
            self._switch_to_session(new_session)

    def action_prev_tab(self) -> None:
        """Switch to previous tab, or toggle inbox pane.

        Flow: choices → inbox → collapsed → choices (via l)
        """
        session = self._focused()
        if session and (session.input_mode or session.voice_recording):
            return

        # If inbox pane is visible and focused, collapse it
        if self._inbox_pane_visible() and self._inbox_pane_focused:
            self._inbox_collapsed = True
            self.query_one("#inbox-list").display = False
            self._inbox_pane_focused = False
            self.query_one("#choices", ListView).focus()
            self._speak_ui("Inbox collapsed")
            return

        # If inbox pane is visible and we're in choices, switch to inbox pane
        if self._inbox_pane_visible() and not self._inbox_pane_focused:
            self._inbox_pane_focused = True
            inbox_list = self.query_one("#inbox-list", ListView)
            inbox_list.focus()
            self._speak_ui("Inbox")
            return

        if self.manager.count() <= 1:
            return
        new_session = self.manager.prev_tab()
        if new_session and new_session.session_id != (session.session_id if session else None):
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
            self._speak_ui("No other tabs with choices")

    def action_toggle_sidebar(self) -> None:
        """Toggle the inbox sidebar collapsed/expanded."""
        session = self._focused()
        if session and (session.input_mode or session.voice_recording):
            return
        if self._inbox_collapsed:
            self._inbox_collapsed = False
            self._update_inbox_list()
            self._speak_ui("Inbox expanded")
        else:
            self._inbox_collapsed = True
            try:
                self.query_one("#inbox-list").display = False
            except Exception:
                pass
            self._inbox_pane_focused = False
            self.query_one("#choices", ListView).focus()
            self._speak_ui("Inbox collapsed")

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
                entry = voice_rot[session_idx % len(voice_rot)]
                session.voice_override = entry.get("voice")
                if entry.get("model"):
                    session.model_override = entry["model"]
            if emotion_rot:
                session.emotion_override = emotion_rot[session_idx % len(emotion_rot)]

        try:
            self.call_from_thread(self._update_tab_bar)
        except Exception:
            pass

        # Update UI to show agent connected (replaces "Waiting for agent...")
        try:
            self.call_from_thread(self._show_idle)
        except Exception:
            pass

        # Speak the connection
        try:
            self._speak_ui(f"{session.name} connected")
        except Exception:
            pass

        # Emit for remote frontends
        try:
            frontend_api.emit_session_created(session.session_id, session.name)
        except Exception:
            pass

        # Notification webhook
        try:
            self._notifier.notify(NotificationEvent(
                event_type="agent_connected",
                title=f"Agent connected: {session.name}",
                message=f"New agent session '{session.name}' registered.",
                session_name=session.name,
                session_id=session.session_id,
                priority=2,
                tags=["new", "robot_face"],
            ))
        except Exception:
            pass

    def on_session_removed(self, session_id: str) -> None:
        """Called when a session is removed."""
        # Capture name before removal for notification
        removed_session = self.manager.get(session_id)
        removed_name = removed_session.name if removed_session else session_id

        new_active = self.manager.remove(session_id)

        # Emit for remote frontends
        try:
            frontend_api.emit_session_removed(session_id)
        except Exception:
            pass

        # Notification webhook
        try:
            self._notifier.notify(NotificationEvent(
                event_type="agent_disconnected",
                title=f"Agent disconnected: {removed_name}",
                message=f"Agent session '{removed_name}' was removed.",
                session_name=removed_name,
                session_id=session_id,
                priority=2,
                tags=["wave"],
            ))
        except Exception:
            pass

        try:
            self.call_from_thread(self._update_tab_bar)
            if new_active is not None:
                new_session = self.manager.get(new_active)
                if new_session:
                    self.call_from_thread(lambda s=new_session: self._switch_to_session(s))
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
        self._replay_prompt_worker(session)

    @work(thread=True, exit_on_error=False, name="replay_prompt", exclusive=True)
    def _replay_prompt_worker(self, session: Session) -> None:
        """Worker: replay preamble and all options in background thread."""
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

    # ─── Voice input ──────────────────────────────────────────────
    # Defined in VoiceMixin (tui/voice.py)

    # ─── Settings menu ────────────────────────────────────────────
    # Defined in SettingsMixin (tui/settings_menu.py)

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

        # Inbox list highlight: read preamble preview of highlighted item
        if isinstance(event.item, InboxListItem):
            preamble = event.item.inbox_preamble if event.item.inbox_preamble else "no preamble"
            n = event.item.n_choices
            status = "done" if event.item.is_done else f"{n} option{'s' if n != 1 else ''}"
            # Include agent name in TTS when in multi-agent mode
            agent_prefix = f"{event.item.session_name}. " if event.item.session_name else ""
            text = f"{agent_prefix}{preamble}. {status}"
            # Deduplicate with cooldown — skip if same text was spoken very recently
            now = time.time()
            last_time = getattr(self, '_last_inbox_spoken_time', 0.0)
            last_text = getattr(self, '_last_inbox_spoken_text', '')
            if text != last_text or (now - last_time) > 0.5:
                self._last_inbox_spoken_text = text
                self._last_inbox_spoken_time = now
                # Use local fallback for instant readout when scrolling inbox
                self._tts.speak_with_local_fallback(text)
            # Track scroll position in inbox
            self._inbox_scroll_index = event.item.inbox_index
            return

        session = self._focused()

        # In setting edit mode, read the value
        if self._setting_edit_mode:
            if isinstance(event.item, ChoiceItem):
                val = self._setting_edit_values[event.item.display_index] if event.item.display_index < len(self._setting_edit_values) else ""
                self._tts.speak_async(val)
            return

        # In settings mode
        if self._in_settings:
            # Dialog mode: read button labels
            if getattr(self, '_dialog_callback', None):
                if isinstance(event.item, ChoiceItem):
                    buttons = getattr(self, '_dialog_buttons', [])
                    idx = event.item.display_index
                    # First item may be the message (choice_index == 0)
                    if event.item.choice_index == 0:
                        return  # Don't read the message on highlight
                    btn_idx = idx - 1 if any(
                        c.choice_index == 0 for c in self.query_one("#choices", ListView).children
                        if isinstance(c, ChoiceItem)
                    ) else idx
                    if 0 <= btn_idx < len(buttons):
                        btn = buttons[btn_idx]
                        text = f"{btn['label']}. {btn.get('summary', '')}" if btn.get('summary') else btn['label']
                        self._tts.speak_async(text)
                return
            # System logs: read the log entry text
            if getattr(self, '_system_logs_mode', False):
                if isinstance(event.item, ChoiceItem):
                    entries = getattr(self, '_system_log_entries', [])
                    idx = event.item.display_index
                    if idx < len(entries):
                        self._tts.speak_async(entries[idx])
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
            # History mode: read the selection entry
            if getattr(self, '_history_mode', False):
                if isinstance(event.item, ChoiceItem) and session:
                    idx = event.item.display_index
                    history = getattr(session, 'history', [])
                    if idx < len(history):
                        entry = history[idx]
                        text = f"{entry.label}. {entry.summary}" if entry.summary else entry.label
                        self._tts.speak_async(text)
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
        # Layout: idx 0=toggle-all, 1..N=choices, N+1=confirm, N+2=team, N+3=cancel
        if self._multi_select_mode and isinstance(event.item, ChoiceItem):
            idx = event.item.display_index
            num_choices = len(session.choices)
            checked_count = sum(self._multi_select_checked) if self._multi_select_checked else 0
            if idx == 0:
                # Select all / Deselect all
                all_selected = all(self._multi_select_checked) if self._multi_select_checked else False
                self._tts.speak_async("Deselect all" if all_selected else "Select all")
            elif 1 <= idx <= num_choices:
                choice_idx = idx - 1
                check = "checked" if (choice_idx < len(self._multi_select_checked) and self._multi_select_checked[choice_idx]) else "unchecked"
                label = session.choices[choice_idx].get("label", "")
                self._tts.speak_async(f"{label}, {check}")
            elif idx == num_choices + 1:
                self._tts.speak_async(f"Confirm. {checked_count} selected.")
            elif idx == num_choices + 2:
                self._tts.speak_async(f"Team mode. {checked_count} for parallel agents.")
            elif idx == num_choices + 3:
                self._speak_ui("Cancel.")
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
                # Extra option — use the widget's label directly
                text = event.item.choice_label
                if event.item.choice_summary:
                    text = f"{text}. {event.item.choice_summary}"
            if text:
                # Deduplicate with cooldown — skip if same text was spoken very recently
                # but allow re-reading after a brief pause (e.g. scrolling away and back)
                now = time.time()
                last_time = getattr(self, '_last_spoken_time', 0.0)
                if text != self._last_spoken_text or (now - last_time) > 0.5:
                    self._last_spoken_text = text
                    self._last_spoken_time = now
                    # Use espeak fallback for instant readout when scrolling options
                    self._tts.speak_with_local_fallback(text)

            if self._dwell_time > 0:
                self._start_dwell()

    @on(ListView.Selected)
    def on_list_selected(self, event: ListView.Selected) -> None:
        """Handle Enter/click on a list item."""
        # Check if this is an inbox list selection (left pane)
        try:
            inbox_list = self.query_one("#inbox-list", ListView)
            if event.list_view is inbox_list and isinstance(event.item, InboxListItem):
                self._handle_inbox_select(event.item)
                return
        except Exception:
            pass

        session = self._focused()
        if self._setting_edit_mode:
            self._apply_setting_edit()
            return
        # Multi-select mode: toggle or confirm
        if self._multi_select_mode and isinstance(event.item, ChoiceItem):
            self._handle_multi_select_enter(event.item.display_index)
            return
        # Dialog mode: dispatch to dialog handler
        if getattr(self, '_dialog_callback', None) and isinstance(event.item, ChoiceItem):
            self._handle_dialog_select(event.item.display_index)
            return
        if self._in_settings:
            if isinstance(event.item, ChoiceItem):
                idx = event.item.display_index
                # Check if we're in spawn menu
                spawn_opts = getattr(self, '_spawn_options', None)
                if spawn_opts and idx < len(spawn_opts):
                    self._clear_all_modal_state(session=session)
                    self._do_spawn(spawn_opts[idx])
                    return
                # Check if we're in system logs mode (Enter closes it)
                if getattr(self, '_system_logs_mode', False):
                    self._system_logs_mode = False
                    self._exit_settings()
                    return
                # Check if we're in help mode (Enter closes it)
                if getattr(self, '_help_mode', False):
                    self._help_mode = False
                    self._exit_settings()
                    return
                # Check if we're in history mode (Enter closes it)
                if getattr(self, '_history_mode', False):
                    self._history_mode = False
                    self._exit_settings()
                    return
                # Check if we're in tab picker mode
                if getattr(self, '_tab_picker_mode', False):
                    sessions = getattr(self, '_tab_picker_sessions', [])
                    self._clear_all_modal_state(session=session)
                    if idx < len(sessions):
                        self._switch_to_session(sessions[idx])
                    return
                # Check if we're in worktree mode
                if getattr(self, '_worktree_options', None):
                    self._handle_worktree_select(idx)
                    return
                # Check if we're in quick settings submenu
                if getattr(self, '_quick_settings_mode', False):
                    items = ["Fast toggle", "Voice toggle", "Notifications", "View logs", "Settings", "Restart proxy", "Restart TUI", "Back"]
                    if idx < len(items):
                        self._handle_quick_settings_select(items[idx])
                    return
                # Check if we're in quick action menu
                qa_opts = getattr(self, '_quick_action_options', None)
                if qa_opts and idx < len(qa_opts):
                    action = qa_opts[idx].get("_action")
                    self._clear_all_modal_state(session=session)
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
        """Interrupt intro/options readout when user scrolls.

        stop() is non-blocking (runs kills in a background thread),
        so this is safe to call from the main event loop.
        """
        session = self._focused()
        if session:
            if getattr(session, 'intro_speaking', False):
                session.intro_speaking = False
                self._tts.stop()
            if getattr(session, 'reading_options', False):
                session.reading_options = False
                self._tts.stop()

    def _sync_inbox_focus_from_widget(self) -> None:
        """Sync _inbox_pane_focused with actual widget focus.

        Called when focus changes (e.g. via Tab key or click) to keep
        the logical state in sync with Textual's widget focus state.
        """
        if not self._inbox_pane_visible():
            return
        try:
            inbox_list = self.query_one("#inbox-list", ListView)
            choices_list = self.query_one("#choices", ListView)
            if inbox_list.has_focus:
                self._inbox_pane_focused = True
            elif choices_list.has_focus:
                self._inbox_pane_focused = False
        except Exception:
            pass

    def _active_list_view(self) -> ListView:
        """Get the currently focused list view (inbox or choices).

        Checks actual widget focus first to handle Tab key / click focus
        changes, then falls back to the logical _inbox_pane_focused state.
        """
        if self._inbox_pane_visible():
            try:
                inbox_list = self.query_one("#inbox-list", ListView)
                choices_list = self.query_one("#choices", ListView)
                # If one of them has actual focus, use that and sync state
                if inbox_list.has_focus:
                    self._inbox_pane_focused = True
                    return inbox_list
                elif choices_list.has_focus:
                    self._inbox_pane_focused = False
                    return choices_list
                # Neither has focus — use logical state
                if self._inbox_pane_focused:
                    return inbox_list
            except Exception:
                pass
        return self.query_one("#choices", ListView)

    def action_cursor_down(self) -> None:
        session = self._focused()
        if session and (session.input_mode or session.voice_recording):
            return
        self._interrupt_readout()
        list_view = self._active_list_view()
        if list_view.display:
            if not list_view.has_focus:
                list_view.focus()
            list_view.action_cursor_down()

    def action_cursor_up(self) -> None:
        session = self._focused()
        if session and (session.input_mode or session.voice_recording):
            return
        self._interrupt_readout()
        list_view = self._active_list_view()
        if list_view.display:
            if not list_view.has_focus:
                list_view.focus()
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
        if (self._in_settings or self._setting_edit_mode or session) and self._scroll_allowed():
            self._interrupt_readout()
            list_view = self._active_list_view()
            if list_view.display:
                if not list_view.has_focus:
                    list_view.focus()
                if self._invert_scroll:
                    list_view.action_cursor_up()
                else:
                    list_view.action_cursor_down()
                event.prevent_default()
                event.stop()

    def on_mouse_scroll_up(self, event: MouseScrollUp) -> None:
        session = self._focused()
        if (self._in_settings or self._setting_edit_mode or session) and self._scroll_allowed():
            self._interrupt_readout()
            list_view = self._active_list_view()
            if list_view.display:
                if not list_view.has_focus:
                    list_view.focus()
                if self._invert_scroll:
                    list_view.action_cursor_down()
                else:
                    list_view.action_cursor_up()
                event.prevent_default()
                event.stop()

    def action_select(self) -> None:
        """Handle Enter key for non-ListView contexts.

        ListView selections are handled by on_list_selected (ListView.Selected event).
        This method only handles cases where Enter has meaning outside the list:
        voice recording stop, setting edit apply.
        """
        if self._setting_edit_mode:
            self._apply_setting_edit()
            return
        session = self._focused()
        # Enter stops voice recording (same as space)
        if session and session.voice_recording:
            self._stop_voice_recording()
            return
        # All other Enter handling (choices, settings menu, activity feed, etc.)
        # is done by on_list_selected via the ListView.Selected event.

    def action_freeform_input(self) -> None:
        """Switch to freeform text input mode using a popup modal."""
        session = self._focused()
        if not session or not session.active or session.input_mode or session.voice_recording:
            return
        session.input_mode = True
        self._freeform_spoken_pos = 0
        session.reading_options = False
        self._cancel_dwell()

        self._tts.stop()
        self._speak_ui("Type your reply")

        def _on_text_changed(text: str) -> None:
            """TTS readback of typed text at delimiter boundaries."""
            if len(text) <= self._freeform_spoken_pos:
                self._freeform_spoken_pos = len(text)
                return
            if text and text[-1] in self._freeform_delimiters:
                chunk = text[self._freeform_spoken_pos:].strip()
                if chunk:
                    self._freeform_tts.stop()
                    self._freeform_tts.speak_with_local_fallback(chunk)
                self._freeform_spoken_pos = len(text)

        def _on_modal_dismiss(result: str | None) -> None:
            """Handle the modal result for freeform input."""
            session.input_mode = False
            if result is None:
                # Cancelled
                self._freeform_tts.stop()
                if session.active:
                    self._show_choices()
                self._speak_ui("Cancelled.")
            else:
                # Submitted
                self._freeform_tts.stop()
                self._tts.stop()
                self._vibrate(100)
                self._tts.speak_async(f"Selected: {result}")
                self._resolve_selection(session, {"selected": result, "summary": "(freeform input)"})
                self._show_waiting(result)

        self.push_screen(
            TextInputModal(
                title="Type your reply",
                message_mode=False,
                scheme=self._cs,
                on_text_changed=_on_text_changed,
            ),
            callback=_on_modal_dismiss,
        )

    def action_queue_message(self) -> None:
        """Open text input modal to queue a message for the agent's next response.
        Also supports voice input — press space to record a voice message.

        Routes to the inbox-highlighted session if inbox is visible,
        otherwise to the active (focused) session.
        """
        session = self._message_target()
        if not session:
            return
        # Allow queueing even when session is not active (agent is working)
        if getattr(session, 'input_mode', False) or getattr(session, 'voice_recording', False):
            return
        self._message_mode = True
        self._message_target_session = session
        self._freeform_spoken_pos = 0
        self._inbox_was_visible = self._inbox_pane_visible()

        self._tts.stop()
        self._speak_ui("Type or speak a message for the agent")

        def _on_text_changed(text: str) -> None:
            """TTS readback of typed text at delimiter boundaries."""
            if len(text) <= self._freeform_spoken_pos:
                self._freeform_spoken_pos = len(text)
                return
            if text and text[-1] in self._freeform_delimiters:
                chunk = text[self._freeform_spoken_pos:].strip()
                if chunk:
                    self._freeform_tts.stop()
                    self._freeform_tts.speak_with_local_fallback(chunk)
                self._freeform_spoken_pos = len(text)

        def _on_modal_dismiss(result: str | None) -> None:
            """Handle the modal result for message queueing."""
            inbox_was_visible = self._inbox_was_visible
            is_interrupt = getattr(self, '_interrupt_mode', False)

            if result == VOICE_REQUESTED:
                # User pressed space — start voice recording.
                # _message_mode stays True so _handle_transcript queues the message.
                self.action_voice_input()
                return

            # Clear message mode state
            self._message_mode = False
            self._interrupt_mode = False
            self._inbox_was_visible = False
            target = self._message_target_session or session
            self._message_target_session = None

            if result is None:
                # Cancelled
                self._freeform_tts.stop()
                if target.active:
                    self._show_choices()
                else:
                    self._ensure_main_content_visible(show_inbox=inbox_was_visible)
                    self._show_session_waiting(target)
                self._speak_ui("Cancelled.")
            else:
                # Submitted
                self._freeform_tts.stop()
                self._tts.stop()
                self._vibrate(100)

                if is_interrupt:
                    self._send_to_agent_pane(target, result)
                else:
                    msgs = getattr(target, 'pending_messages', None)
                    if msgs is not None:
                        msgs.append(result)
                    count = len(msgs) if msgs else 1
                    agent_name = target.name or "agent"
                    self._speak_ui(f"Message queued for {agent_name}. {count} pending.")

                if target.active:
                    self._show_choices()
                else:
                    self._ensure_main_content_visible(show_inbox=inbox_was_visible)
                    self._show_session_waiting(target)

        self.push_screen(
            TextInputModal(
                title="Type or speak a message",
                message_mode=True,
                scheme=self._cs,
                on_text_changed=_on_text_changed,
            ),
            callback=_on_modal_dismiss,
        )

    def action_voice_message(self) -> None:
        """Start voice recording directly in message mode.

        Like pressing m then space, but as a single key (M). Records voice,
        transcribes, and queues the result as a pending message for the agent.
        Routes to inbox-highlighted session if inbox is visible.
        """
        session = self._message_target()
        if not session:
            return
        if getattr(session, 'input_mode', False) or getattr(session, 'voice_recording', False):
            return
        self._message_mode = True
        self._message_target_session = session  # store target for submission
        self._freeform_spoken_pos = 0
        self._inbox_was_visible = self._inbox_pane_visible()
        self._tts.stop()
        self._speak_ui("Recording voice message")
        self._start_voice_recording()

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

        self._speak_ui("Type to filter choices")

    def _apply_filter(self, query: str) -> None:
        """Filter the choices ListView to show only matching items."""
        session = self._focused()
        if not session or not session.active:
            return

        list_view = self.query_one("#choices", ListView)
        list_view.clear()
        q = query.lower()

        # Build visible extras based on expand/collapse state
        if self._extras_expanded:
            visible_extras = list(SECONDARY_EXTRAS) + list(PRIMARY_EXTRAS)
        else:
            visible_extras = [MORE_OPTIONS_ITEM] + list(PRIMARY_EXTRAS)

        # Always show extras (but filtered too if query is set)
        for i, e in enumerate(visible_extras):
            logical_idx = -(len(visible_extras) - 1 - i)
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
                index=i + 1, display_index=len(visible_extras) + i,
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
        self._speak_ui(f"{count} matches")

    # NOTE: Old on_freeform_changed, on_freeform_submitted, _submit_freeform,
    # and _cancel_freeform have been removed. Text input now uses TextInputModal
    # (a Textual ModalScreen) which handles its own submit/cancel/change events.
    # See action_freeform_input() and action_queue_message().

    def on_key(self, event) -> None:
        """Handle Escape in voice/settings/filter mode."""
        session = self._focused()
        if self._filter_mode and event.key == "escape":
            self._exit_filter()
            self._speak_ui("Filter cleared")
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
            self._speak_ui("Recording cancelled")
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
        """Immediately select option by 1-based number.

        Works in all menus: regular choices, activity feed, quick settings,
        dashboard, settings, dialogs, spawn menu, tab picker, and setting
        edit mode. Blocked only during text input and voice recording.
        """
        session = self._focused()
        if not session:
            return
        if session.input_mode or session.voice_recording:
            return

        # Quick settings submenu — dispatch by number
        if getattr(self, '_quick_settings_mode', False):
            items = ["Fast toggle", "Voice toggle", "Notifications", "View logs", "Settings", "Restart proxy", "Restart TUI", "Back"]
            if 1 <= n <= len(items):
                self._handle_quick_settings_select(items[n - 1])
            return

        # Settings menu — select setting by number
        if self._in_settings:
            # Tab picker
            if getattr(self, '_tab_picker_mode', False):
                sessions = getattr(self, '_tab_picker_sessions', [])
                if 1 <= n <= len(sessions):
                    session = self._focused()
                    self._clear_all_modal_state(session=session)
                    self._switch_to_session(sessions[n - 1])
                return

            # Spawn agent menu
            spawn_opts = getattr(self, '_spawn_options', None)
            if spawn_opts:
                if 1 <= n <= len(spawn_opts):
                    session = self._focused()
                    self._clear_all_modal_state(session=session)
                    self._do_spawn(spawn_opts[n - 1])
                return

            # Quick action menu
            qa_opts = getattr(self, '_quick_action_options', None)
            if qa_opts:
                if 1 <= n <= len(qa_opts):
                    action = qa_opts[n - 1].get("_action")
                    session = self._focused()
                    self._clear_all_modal_state(session=session)
                    if action is None:
                        self._exit_settings()
                    else:
                        self._execute_quick_action(action)
                return

            # Worktree options
            if getattr(self, '_worktree_options', None):
                wt_opts = self._worktree_options
                if 1 <= n <= len(wt_opts):
                    self._handle_worktree_select(n - 1)
                return

            # Dialog (quit confirm, restart confirm, etc.)
            dialog_cb = getattr(self, '_dialog_callback', None)
            dialog_btns = getattr(self, '_dialog_buttons', None)
            if dialog_cb and dialog_btns:
                if 1 <= n <= len(dialog_btns):
                    # Buttons are at display_index 1+ (index 0 is the message)
                    # Use n directly as display_index since message is at 0
                    self._handle_dialog_select(n)
                return

            # Help, history, log — read-only, no action on number
            if getattr(self, '_help_mode', False):
                return
            if getattr(self, '_system_logs_mode', False):
                return
            if getattr(self, '_history_mode', False):
                return

            # Settings items (Speed, Voice, Emotion, etc.)
            if hasattr(self, '_settings_items') and self._settings_items:
                if 1 <= n <= len(self._settings_items):
                    key = self._settings_items[n - 1]["key"]
                    if key == "close":
                        self._exit_settings()
                    else:
                        self._enter_setting_edit(key)
                return

            # Setting edit mode — number picks from value list
            if self._setting_edit_mode:
                list_view = self.query_one("#choices", ListView)
                if 1 <= n <= len(list_view.children):
                    list_view.index = n - 1
                    self._apply_setting_edit()
                return

            return

        # Activity feed — dispatch actionable items by number
        if not session.active and not self._in_settings:
            list_view = self.query_one("#choices", ListView)
            if n - 1 < len(list_view.children):
                list_view.index = n - 1
                # Trigger _do_select which handles activity feed items
                self._do_select()
            return

        # Regular choices mode
        if not session.active or self._in_settings:
            return

        # Multi-select mode: toggle checkbox instead of selecting
        if self._multi_select_mode:
            # n is 1-based choice number
            choice_idx = n - 1
            if 0 <= choice_idx < len(self._multi_select_checked):
                self._multi_select_checked[choice_idx] = not self._multi_select_checked[choice_idx]
                label = session.choices[choice_idx].get("label", "")
                state = "selected" if self._multi_select_checked[choice_idx] else "unselected"
                self._tts.speak_async(f"{label} {state}")
                self._refresh_multi_select()
            return

        # Calculate the actual number of displayed extras (collapsed vs expanded)
        if self._extras_expanded:
            n_visible_extras = len(SECONDARY_EXTRAS) + len(PRIMARY_EXTRAS)
        else:
            n_visible_extras = 1 + len(PRIMARY_EXTRAS)  # "More options" + primary

        display_idx = n_visible_extras + n - 1
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
        """Context-aware quit: back/escape in modal views, exit at top level."""
        self._quit_or_back()

    def action_quit(self) -> None:
        """Context-aware quit (bound to configurable quit key)."""
        self._quit_or_back()

    def _quit_or_back(self) -> None:
        """If in a modal (settings, dashboard, log, filter, multi-select), go back.
        If at the top level (idle or viewing choices), actually quit."""

        # Multi-select mode → cancel multi-select
        if self._multi_select_mode:
            self._multi_select_mode = False
            self._multi_select_checked = []
            self._show_choices()
            self._speak_ui("Multi-select cancelled.")
            return

        # Filter mode → exit filter
        if self._filter_mode:
            self._filter_mode = False
            filter_input = self.query_one("#filter-input", Input)
            filter_input.value = ""
            filter_input.styles.display = "none"
            self._show_choices()
            return

        # Settings / help / any modal → back
        if self._in_settings:
            self._exit_settings()
            return

        # Conversation mode → exit conversation
        if self._conversation_mode:
            self._conversation_mode = False
            self._tts.play_chime("convo_off")
            self._speak_ui("Conversation mode off.")
            session = self._focused()
            if session and session.active:
                self._show_choices()
            return

        # Session has active input mode → dismiss modal if present
        session = self._focused()
        if session and session.input_mode:
            session.input_mode = False
            # If a TextInputModal is on screen, pop it
            if isinstance(self.screen, TextInputModal):
                self.screen.dismiss(None)
            elif session.active:
                self._show_choices()
            return

        # Top level: confirm before quitting
        def _on_quit_confirm(label: str):
            if label.lower().startswith("quit"):
                for sess in self.manager.all_sessions():
                    if sess.active:
                        self._cancel_dwell()
                        self._resolve_selection(sess, {"selected": "quit", "summary": "User quit"})
                self.exit()
            else:
                self._exit_settings()

        self._show_dialog(
            title="Quit io-mcp?",
            message="The TUI will close. Proxy and agent connections stay alive.",
            buttons=[
                {"label": "Quit", "summary": "Exit io-mcp TUI"},
                {"label": "Cancel", "summary": "Go back"},
            ],
            callback=_on_quit_confirm,
        )

    def action_hot_reload(self) -> None:
        """Refresh the TUI state — config, tab bar, activity feeds, inboxes.

        Does NOT monkey-patch code. For code changes, restart the TUI instead.
        Reloads config from disk, clears TTS cache, refreshes the UI.
        """
        self._tts.stop()

        try:
            # Ensure TTS is unmuted
            self._tts._muted = False

            # Reload config from disk
            if self._config:
                self._config.reload()
                self._tts.clear_cache()

            # Refresh the tab bar
            self._update_tab_bar()

            # Refresh the current view
            session = self._focused()
            if session:
                if session.active:
                    self._show_choices()
                else:
                    self._show_session_waiting(session)

            self._speak_ui("Refreshed")
        except Exception as e:
            self._tts.speak_async(f"Refresh failed: {str(e)[:80]}")

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
            label = item.choice_label
            self._handle_extra_select(label)
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

        self._resolve_selection(session, {"selected": label, "summary": summary})

        # Emit event for remote frontends
        try:
            frontend_api.emit_selection_made(session.session_id, label, summary)
        except Exception:
            pass

        self._show_waiting(label)

    def _handle_extra_select(self, label: str) -> None:
        """Handle selection of extra options by label."""
        self._tts.stop()
        self._vibrate(100)  # Haptic feedback on extra selection

        if label == "More options ›" or label == "More options":
            # Toggle expand/collapse and re-render
            self._extras_expanded = not self._extras_expanded
            self._show_choices()
            if self._extras_expanded:
                self._speak_ui("More options")
            else:
                self._speak_ui("Collapsed")
            return

        if label == "Record response":
            self.action_voice_input()
        elif label == "Multi select":
            self._enter_multi_select_mode()
        elif label == "Interrupt agent":
            self._action_interrupt_agent()
        elif label == "Branch to worktree":
            self._enter_worktree_mode()
        elif label == "Compact context":
            self._request_compact()
        elif label == "Pane view":
            self.action_pane_view()
        elif label == "Switch tab":
            self._enter_tab_picker()
        elif label == "New agent":
            self.action_spawn_agent()
        elif label == "View logs":
            self.action_view_system_logs()
        elif label == "Close tab":
            session = self._focused()
            if session:
                self._close_session(session)
        elif label == "Quick settings":
            self._enter_quick_settings()
        elif label == "History":
            self._show_history()
        elif label == "Queue message":
            self.action_queue_message()

    def _enter_quick_settings(self) -> None:
        """Show quick settings submenu with speed/voice toggles, settings, restart, etc."""
        self._tts.stop()
        self._speak_ui("Quick settings")
        self._in_settings = True
        self._setting_edit_mode = False
        self._quick_settings_mode = True

        s = self._cs
        preamble_widget = self.query_one("#preamble", Label)
        preamble_widget.update("Quick Settings")
        preamble_widget.display = True
        self.query_one("#status").display = False

        self._ensure_main_content_visible(show_inbox=False)

        items = [
            {"label": "Fast toggle", "summary": f"Toggle speed (current: {self.settings.speed:.1f}x)"},
            {"label": "Voice toggle", "summary": f"Quick-switch voice (current: {self.settings.voice})"},
            {"label": "Notifications", "summary": "Check Android notifications"},
            {"label": "View logs", "summary": "TUI errors, proxy logs, speech history"},
            {"label": "Settings", "summary": "Open full settings menu"},
            {"label": "Restart proxy", "summary": "Kill and restart MCP proxy (agents reconnect)"},
            {"label": "Restart TUI", "summary": "Restart the TUI backend"},
        ]

        # Add djent swarm controls when djent integration is enabled
        if self._config and self._config.djent_enabled:
            items.extend([
                {"label": "Swarm status", "summary": "Show djent agent status and project overview"},
                {"label": "Start swarm", "summary": "Launch the djent dev loop in a new tmux window"},
                {"label": "Stop swarm", "summary": "Gracefully stop all djent agents"},
                {"label": "View agent logs", "summary": "Tail recent djent agent log output"},
            ])

        items.append({"label": "Back", "summary": "Return to choices"})

        list_view = self.query_one("#choices", ListView)
        list_view.clear()
        for i, item in enumerate(items):
            list_view.append(ChoiceItem(
                item["label"], item["summary"],
                index=i + 1, display_index=i,
            ))
        list_view.display = True
        list_view.index = 0
        list_view.focus()

    def _handle_quick_settings_select(self, label: str) -> None:
        """Handle selection in the quick settings submenu."""
        self._tts.stop()
        self._vibrate(100)
        self._quick_settings_mode = False

        if label == "Fast toggle":
            msg = self.settings.toggle_fast()
            self._tts.clear_cache()
            self._speak_ui(msg)
            self._enter_quick_settings()  # Stay in submenu
        elif label == "Voice toggle":
            msg = self.settings.toggle_voice()
            self._tts.clear_cache()
            self._speak_ui(msg)
            self._enter_quick_settings()  # Stay in submenu
        elif label == "Notifications":
            session = self._focused()
            self._clear_all_modal_state(session=session)
            self._show_notifications()
        elif label == "View logs":
            self._in_settings = False
            self.action_view_system_logs()
        elif label == "Settings":
            session = self._focused()
            self._clear_all_modal_state(session=session)
            self._enter_settings()
        elif label == "Restart proxy":
            session = self._focused()
            self._clear_all_modal_state(session=session)
            self._restart_proxy_from_tui()
        elif label == "Restart TUI":
            session = self._focused()
            self._clear_all_modal_state(session=session)
            self._restart_tui()
        elif label == "Swarm status":
            self._run_djent_command("Swarm status", "djent status 2>&1 | head -40")
        elif label == "Start swarm":
            self._start_djent_swarm()
        elif label == "Stop swarm":
            self._stop_djent_swarm()
        elif label == "View agent logs":
            self._run_djent_command("Agent logs", "djent log 2>&1 | tail -20")
        else:
            # "Back" or unknown
            self._exit_settings()

    def _run_djent_command(self, label: str, command: str) -> None:
        """Run a djent CLI command via worker, speak the output, return to quick settings."""
        self._speak_ui(f"Running {label}")
        self._run_djent_command_worker(label, command)

    @work(thread=True, exit_on_error=False, name="djent_command")
    def _run_djent_command_worker(self, label: str, command: str) -> None:
        """Worker: run djent command in background thread."""
        try:
            result = subprocess.run(
                command, shell=True, capture_output=True, text=True, timeout=30
            )
            # Strip ANSI escape codes for clean TTS output
            import re as _re
            output = _re.sub(r'\x1b\[[0-9;]*m', '', result.stdout.strip() or result.stderr.strip())
            if result.returncode == 0:
                summary = output[:300] if output else "Done"
                self._tts.speak_async(f"{label}: {summary}")
            else:
                err = output[:150] if output else f"exit code {result.returncode}"
                self._tts.speak_async(f"{label} failed: {err}")
        except subprocess.TimeoutExpired:
            self._tts.speak_async(f"{label} timed out after 30 seconds")
        except Exception as e:
            self._tts.speak_async(f"Error running {label}: {str(e)[:80]}")

        self.call_from_thread(self._enter_quick_settings)

    def _start_djent_swarm(self) -> None:
        """Start the djent dev loop in a new tmux window with confirmation."""

        def _on_confirm(label: str):
            if label.lower().startswith("start"):
                self._speak_ui("Starting djent swarm")
                self._start_djent_swarm_worker()
            else:
                self._enter_quick_settings()

        self._show_dialog(
            title="Start Djent Swarm?",
            message="This will launch the djent dev loop in a new tmux window.",
            buttons=[
                {"label": "Start swarm", "summary": "Launch djent -e '(loop/dev)' in new tmux window"},
                {"label": "Cancel", "summary": "Go back to quick settings"},
            ],
            callback=_on_confirm,
        )

    @work(thread=True, exit_on_error=False, name="djent_swarm_start")
    def _start_djent_swarm_worker(self) -> None:
        """Worker: start djent swarm in background thread."""
        try:
            result = subprocess.run(
                "tmux new-window -n djent 'djent -e \"(loop/dev)\"'",
                shell=True, capture_output=True, text=True, timeout=15
            )
            if result.returncode == 0:
                self._tts.speak_async("Djent swarm started in new tmux window")
            else:
                err = result.stderr.strip()[:100] or f"exit code {result.returncode}"
                self._tts.speak_async(f"Failed to start swarm: {err}")
        except Exception as e:
            self._tts.speak_async(f"Error starting swarm: {str(e)[:80]}")

        self.call_from_thread(self._enter_quick_settings)

    def _stop_djent_swarm(self) -> None:
        """Stop all djent agents with confirmation."""

        def _on_confirm(label: str):
            if label.lower().startswith("stop"):
                self._speak_ui("Stopping djent swarm")
                self._stop_djent_swarm_worker()
            else:
                self._enter_quick_settings()

        self._show_dialog(
            title="Stop Djent Swarm?",
            message="This will gracefully stop all djent agents and reset bead states.",
            buttons=[
                {"label": "Stop swarm", "summary": "Run djent down to stop all agents"},
                {"label": "Cancel", "summary": "Go back to quick settings"},
            ],
            callback=_on_confirm,
        )

    @work(thread=True, exit_on_error=False, name="djent_swarm_stop")
    def _stop_djent_swarm_worker(self) -> None:
        """Worker: stop djent swarm in background thread."""
        try:
            import re as _re
            result = subprocess.run(
                "djent down 2>&1",
                shell=True, capture_output=True, text=True, timeout=30
            )
            output = _re.sub(r'\x1b\[[0-9;]*m', '', result.stdout.strip() or result.stderr.strip())
            if result.returncode == 0:
                summary = output[:200] if output else "Done"
                self._tts.speak_async(f"Swarm stopped. {summary}")
            else:
                err = output[:100] if output else f"exit code {result.returncode}"
                self._tts.speak_async(f"Stop failed: {err}")
        except subprocess.TimeoutExpired:
            self._tts.speak_async("Stop command timed out after 30 seconds")
        except Exception as e:
            self._tts.speak_async(f"Error stopping swarm: {str(e)[:80]}")

        self.call_from_thread(self._enter_quick_settings)

    def _show_history(self) -> None:
        """Show selection history for the focused session in a scrollable list.

        Displays entries with timestamps, labels, and preambles.
        Each entry is read aloud when highlighted. Press Escape to return.
        """
        session = self._focused()
        if not session:
            self._speak_ui("No session active")
            return

        history = getattr(session, 'history', [])
        if not history:
            self._speak_ui("No history yet for this session")
            return

        # Toggle off if already in history mode
        if getattr(self, '_history_mode', False):
            self._history_mode = False
            self._exit_settings()
            return

        self._tts.stop()

        # Enter history mode (uses settings infrastructure for modal display)
        self._in_settings = True
        self._setting_edit_mode = False
        self._history_mode = True

        s = self._cs
        count = len(history)
        preamble_widget = self.query_one("#preamble", Label)
        preamble_widget.update(
            f"[bold {s['accent']}]History[/bold {s['accent']}] — "
            f"[{s['fg_dim']}]{session.name}[/{s['fg_dim']}] — "
            f"{count} selection{'s' if count != 1 else ''} "
            f"[dim](esc to close)[/dim]"
        )
        preamble_widget.display = True

        self._ensure_main_content_visible(show_inbox=False)

        list_view = self.query_one("#choices", ListView)
        list_view.clear()

        import time as _time

        for i, entry in enumerate(history):
            age = _time.time() - entry.timestamp
            if age < 60:
                time_str = f"{int(age)}s ago"
            elif age < 3600:
                time_str = f"{int(age)//60}m ago"
            else:
                time_str = f"{int(age)//3600}h{int(age)%3600//60:02d}m ago"

            label = f"[{s['fg_dim']}]{time_str}[/{s['fg_dim']}]  {entry.label}"
            summary = entry.summary if entry.summary else entry.preamble if entry.preamble else ""
            list_view.append(ChoiceItem(label, summary, index=i + 1, display_index=i))

        list_view.display = True
        list_view.index = max(0, len(history) - 1)  # Start at most recent
        list_view.focus()

        self._speak_ui(f"History. {count} selections. Most recent shown.")

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
            self._speak_ui("Already in choices. Scroll to pick.")
            return

        # After selection: check if we have choices to go back to
        last_choices = getattr(session, 'last_choices', [])
        last_preamble = getattr(session, 'last_preamble', '')
        if not last_choices:
            self._speak_ui("Nothing to undo")
            return

        # Set the undo sentinel — the server loop will re-present
        self._vibrate(100)
        self._tts.stop()
        self._speak_ui("Undoing selection")

        # Re-activate the session with the saved choices
        self._resolve_selection(session, {"selected": "_undo", "summary": ""})

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

        self._ensure_main_content_visible(show_inbox=False)

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
        """Execute the agent spawn via a Textual worker."""
        label = option.get("label", "")
        host = option.get("_host", "")
        workdir = option.get("_workdir", "")

        if label == "Cancel":
            self._exit_settings()
            return

        self._speak_ui(f"Spawning {label}")
        self._do_spawn_worker(label, host, workdir)

    @work(thread=True, exit_on_error=False, name="spawn_agent")
    def _do_spawn_worker(self, label: str, host: str, workdir: str) -> None:
        """Worker: spawn agent in background thread."""
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
                    f'claude --agent io-mcp "connect to io-mcp and greet the user"'
                )
                cmd = [
                    tmux, "new-session", "-d", "-s", session_name,
                    f"ssh -t {host} '{remote_cmd}'"
                ]
            else:
                # Local spawn in new tmux session
                workdir_resolved = workdir or (
                    self._config.agent_default_workdir if self._config else "~"
                )
                workdir_expanded = os.path.expanduser(workdir_resolved)
                cmd = [
                    tmux, "new-session", "-d", "-s", session_name,
                    "-c", workdir_expanded,
                    "bash", "-c",
                    f'IO_MCP_URL={io_mcp_url} claude --agent io-mcp "connect to io-mcp and greet the user"',
                ]

            result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            if result.returncode == 0:
                self._speak_ui(f"Agent spawned: {session_name}. It will connect shortly.")
            else:
                err = result.stderr[:100] if result.stderr else "unknown error"
                self._tts.speak_async(f"Spawn failed: {err}")

        except Exception as e:
            self._tts.speak_async(f"Spawn error: {str(e)[:80]}")

        self.call_from_thread(self._exit_settings)

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
            self._speak_ui("Conversation mode on. I'll listen after each response.")
        else:
            self._tts.play_chime("convo_off")
            self._speak_ui("Conversation mode off. Back to choices.")
            # If session is active, restore the choices UI
            if session and session.active:
                self.call_from_thread(self._show_choices)

    # ─── View actions (dashboard, timeline, pane, help) ──────────
    # Defined in ViewsMixin (tui/views.py)

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
            self._speak_ui("No config loaded")
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
            self._run_quick_action_command_worker(label, value)

        else:
            self._tts.speak_async(f"Unknown action type: {action_type}")
            self._exit_settings()

    @work(thread=True, exit_on_error=False, name="quick_action_command")
    def _run_quick_action_command_worker(self, label: str, command: str) -> None:
        """Worker: run quick action command in background thread."""
        try:
            result = subprocess.run(
                command, shell=True, capture_output=True, text=True, timeout=60
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
