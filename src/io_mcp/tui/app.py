"""Main TUI application for io-mcp.

Contains the IoMcpApp (Textual App subclass) and TUI controller wrapper.
"""

from __future__ import annotations

import os
import random
import subprocess
import sys
import threading
import time
from typing import Optional

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.events import MouseScrollDown, MouseScrollUp
from textual.timer import Timer
from textual.widget import Widget
from textual.widgets import Header, Input, Label, ListView, RichLog, Static

from ..session import Session, SessionManager, SpeechEntry, HistoryEntry, InboxItem, _resolve_pending_inbox
from ..settings import Settings
from ..tts import TTSEngine, _find_binary
from .. import api as frontend_api
from .. import state as ui_state
from ..logging import get_logger, log_context, TUI_ERROR_LOG
from ..notifications import (
    NotificationEvent, create_dispatcher,
)

_log = get_logger("io-mcp.tui", TUI_ERROR_LOG)

import re as _re

# Strip Rich markup tags like [bold], [#616e88], [/bold], etc.
_RICH_TAG_RE = _re.compile(r'\[[^\]]*\]')

def _strip_rich_markup(text: str) -> str:
    """Remove Rich markup tags from text for TTS readout."""
    return _RICH_TAG_RE.sub('', text).strip()

from .themes import COLOR_SCHEMES, DEFAULT_SCHEME, get_scheme, build_css
from .widgets import ChoiceItem, InboxListItem, PreambleItem, DwellBar, ManagedListView, TextInputModal, SubmitTextArea, VoiceButton, VOICE_REQUESTED, EXTRA_OPTIONS, PRIMARY_EXTRAS, SECONDARY_EXTRAS, MORE_OPTIONS_ITEM, _safe_action
from .views import ViewsMixin
from .voice import VoiceMixin
from .settings_menu import SettingsMixin
from .chat_view import ChatViewMixin, ChatBubbleItem

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from ..config import IoMcpConfig


# Alias for internal use
_build_css = build_css


# ─── Main TUI App ───────────────────────────────────────────────────────────

class IoMcpApp(ChatViewMixin, ViewsMixin, VoiceMixin, SettingsMixin, App):
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
        Binding("g", "chat_view", "Chat Feed", show=False),
        Binding("b", "toggle_sidebar", "Sidebar", show=False),
        Binding("d", "dismiss_item", "Dismiss", show=False),
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
        dismiss_key = kb.get("dismiss", "d")
        help_key = kb.get("help", "question_mark")
        reload_key = kb.get("refresh", kb.get("hotReload", "r"))
        quit_key = kb.get("quit", "q")

        voice_message_key = kb.get("voiceMessage", "M")

        # Store key labels for footer hints (human-readable versions)
        def _key_label(k: str) -> str:
            """Convert internal key name to display label."""
            return {"enter": "Enter", "space": "Space", "question_mark": "?",
                    "slash": "/"}.get(k, k)
        self._key_labels = {
            "down": _key_label(down_key),
            "up": _key_label(up_key),
            "select": _key_label(select_key),
            "message": _key_label(message_key),
            "settings": _key_label(settings_key),
            "pane": _key_label(pane_key),
            "dismiss": _key_label(dismiss_key),
            "undo": _key_label(undo_key),
            "help": _key_label(help_key),
        }

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
            Binding(dismiss_key, "dismiss_item", "Dismiss", show=False),
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

        # Register TTS error callback for visible error reporting
        self._tts._on_tts_error = self._on_tts_error
        self._last_tts_error: str = ""
        self._last_tts_error_time: float = 0.0
        self._freeform_delimiters = set(freeform_delimiters)
        self._scroll_debounce = scroll_debounce
        self._invert_scroll = invert_scroll
        self._demo = demo
        self._config = config
        self._last_scroll_time: float = 0.0
        self._dwell_time = dwell_time

        # Scroll acceleration — ring buffer of recent scroll timestamps
        sa = config.scroll_acceleration if config else {}
        self._scroll_accel_enabled: bool = bool(sa.get("enabled", True))
        self._scroll_accel_fast_ms: float = float(sa.get("fastThresholdMs", 80))
        self._scroll_accel_turbo_ms: float = float(sa.get("turboThresholdMs", 40))
        self._scroll_accel_fast_skip: int = int(sa.get("fastSkip", 3))
        self._scroll_accel_turbo_skip: int = int(sa.get("turboSkip", 5))
        self._scroll_times: list[float] = []  # last N scroll timestamps

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
        self._chat_extras_expanded = False

        # Dwell timer
        self._dwell_timer: Optional[Timer] = None
        self._dwell_start: float = 0.0

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
        self._inbox_collapsed = ui_state.get("inbox_collapsed", False)  # persistent toggle

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

    def _on_tts_error(self, message: str) -> None:
        """Handle TTS error — show in status line with auto-dismiss.

        Called from the TTSEngine when API TTS fails. Shows a brief
        error message in the TUI status area so the user knows audio
        failed without falling back to local TTS. Auto-dismisses after
        5 seconds to avoid persistent stale errors.
        """
        import time as _time
        self._last_tts_error = message
        self._last_tts_error_time = _time.time()
        error_id = self._last_tts_error_time  # unique ID for this error
        try:
            def _show():
                try:
                    s = self._cs
                    status = self.query_one("#status", Label)
                    status.update(f"[{s['error']}]⚠ TTS: {message[:80]}[/{s['error']}]")
                    status.display = True
                except Exception:
                    pass
            self._safe_call(_show)
            # Auto-dismiss after 5 seconds (only if no newer error has appeared)
            def _dismiss():
                if self._last_tts_error_time == error_id:
                    try:
                        status = self.query_one("#status", Label)
                        status.display = False
                    except Exception:
                        pass
            self.set_timer(5.0, _dismiss)
        except Exception:
            pass

    def _call_on_main_thread(self, callback, *args) -> None:
        """Dispatch a callback to the main Textual thread.

        If already on the main thread, calls directly.
        If on a background thread, uses call_from_thread().
        This avoids the RuntimeError when on_session_created is
        invoked from the main thread (e.g. during tests or initial setup).
        """
        if threading.current_thread() is not threading.main_thread() and self.is_running:
            if args:
                self.call_from_thread(callback, *args)
            else:
                self.call_from_thread(callback)
        else:
            if args:
                callback(*args)
            else:
                callback()

    def _speak_ui(self, text: str) -> None:
        """Speak a UI message (settings, navigation, prompts) with optional separate voice.

        Uses tts.uiVoice from config if set, otherwise falls back to the
        regular voice. This keeps UI narration distinct from agent speech.

        UI speech self-interrupts: stops any current playback (including
        previous UI speech) and plays immediately. This makes menus,
        settings, and dialogs feel responsive — newest UI text wins.
        Uses speak_with_local_fallback for instant cached playback.
        """
        voice_ov = None
        speed_ov = self._config.tts_speed_for("ui") if self._config else None
        if self._config:
            ui_preset = self._config.tts_ui_voice_preset
            # Only override if uiVoice is explicitly set and different from default
            if ui_preset and ui_preset != self._config.tts_voice_preset:
                voice_ov = ui_preset
        self._tts.speak_with_local_fallback(text, voice_override=voice_ov,
                                            speed_override=speed_ov)

    @work(thread=True, exit_on_error=False, group="pregenerate")
    def _pregenerate_worker(self, texts: list[str],
                            speed_override: Optional[float] = None) -> None:
        """Worker: pregenerate TTS clips in background thread."""
        self._tts.pregenerate(texts, speed_override=speed_override)

    @work(thread=True, exit_on_error=False, group="pregenerate-ui")
    def _pregenerate_ui_worker(self, texts: list[str]) -> None:
        """Worker: pregenerate UI TTS clips in separate background queue.

        UI texts (extra options, settings, common messages) use their
        own pregeneration queue so they don't compete with agent choice
        pregeneration for API bandwidth.
        """
        voice_ov = None
        speed_ov = self._config.tts_speed_for("ui") if self._config else None
        if self._config:
            ui_preset = self._config.tts_ui_voice_preset
            if ui_preset and ui_preset != self._config.tts_voice_preset:
                voice_ov = ui_preset
        self._tts.pregenerate_ui(texts, voice_override=voice_ov,
                                 speed_override=speed_ov)

    def _pregenerate_common_ui_texts(self) -> None:
        """Pregenerate common UI phrases, number words, and extra option labels.

        Called on mount and after config reload. Ensures that the most
        frequently heard scroll-wheel TTS strings are cached before
        the user encounters them, maximizing cache hit rates.

        Uses the UI pregeneration queue (low priority, 1 worker).
        """
        from .widgets import PRIMARY_EXTRAS, SECONDARY_EXTRAS, MORE_OPTIONS_ITEM

        ui_texts = set()

        # Common UI phrases (selection feedback, navigation, menus)
        ui_texts.update(TTSEngine._COMMON_UI_PHRASES)

        # Number words ("one" through "nine") — used in scroll readout
        ui_texts.update(TTSEngine._NUMBER_WORDS.values())

        # Settings menu labels
        ui_texts.update(TTSEngine._SETTINGS_LABELS)

        # Quick settings labels
        ui_texts.update(TTSEngine._QUICK_SETTINGS_LABELS)

        # All extra option labels and summaries (primary + secondary + toggle)
        for e in list(PRIMARY_EXTRAS) + list(SECONDARY_EXTRAS) + [MORE_OPTIONS_ITEM]:
            label = e.get('label', '')
            summary = e.get('summary', '')
            if label:
                ui_texts.add(label)
            if summary:
                ui_texts.add(summary)

        if ui_texts:
            self._pregenerate_ui_worker(list(ui_texts))

    def _ensure_main_content_visible(self, show_inbox: bool = False) -> None:
        """Ensure the #main-content container is visible.

        Called before showing the #choices list in any context (settings,
        etc.) since #choices is nested inside #main-content > #choices-panel.

        In chat view mode, this is a no-op for most callers. Only
        _show_choices explicitly shows #main-content in chat view (with
        limited height) when there are active choices to display.

        Args:
            show_inbox: If True, also update and show the inbox list
                       (unless user has collapsed it). If False, hide
                       the inbox list (for modal views).
        """
        _log.info("_ensure_main_content_visible: entering", extra={"context": {
            "show_inbox": show_inbox,
            "chat_view_active": self._chat_view_active,
        }})
        # Chat view: don't show #main-content here — only _show_choices
        # should show it when choices are active. This prevents the
        # waiting state from appearing below the chat feed.
        if self._chat_view_active:
            return

        try:
            mc = self.query_one("#main-content")
            mc.display = True
            mc.styles.height = "1fr"
            mc.styles.max_height = None
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
        # ── Header (hidden via CSS, replaced by #tab-bar) ──
        yield Header(name="io-mcp", show_clock=False)

        # ── Tab bar (dock: top — replaces Header) ──
        with Horizontal(id="tab-bar"):
            yield Static("", id="tab-bar-left")
            yield Static("", id="tab-bar-right")

        # ── Legacy daemon status (display: none — merged into tab bar) ──
        yield Static("", id="daemon-status")

        # ── Main content area (normal flow) ──
        status_text = "[dim]Ready — demo mode[/dim]" if self._demo else "[dim]Waiting for agent...[/dim]"
        yield Label(status_text, id="status")
        yield Label("", id="agent-activity")
        yield Vertical(id="speech-log")

        # ── Two-column inbox layout (choices view) ──
        with Horizontal(id="main-content"):
            yield ManagedListView(id="inbox-list")
            with Vertical(id="choices-panel"):
                yield Label("", id="preamble")
                yield ManagedListView(id="choices")
                yield DwellBar(id="dwell-bar")

        # ── Tmux pane view (display: none, toggled by 'v' key) ──
        yield RichLog(id="pane-view", markup=False, highlight=False, auto_scroll=True, max_lines=200)

        # ── Chat view widgets ──
        yield ManagedListView(id="chat-feed")

        # ── Bottom-docked widgets (last yielded = bottom-most) ──
        # dock: bottom — choices overlay in chat view
        yield ManagedListView(id="chat-choices")
        # dock: bottom — text input + voice button for chat view
        with Horizontal(id="chat-input-bar"):
            yield SubmitTextArea(id="chat-input", placeholder="Type a message...")
            yield VoiceButton("🎤", id="chat-voice-btn")
        # Filter input (display: none by default, toggled by '/' key)
        yield Input(placeholder="Filter choices...", id="filter-input")
        # dock: bottom — persistent status line (must be last for bottom-most position)
        yield Static("", id="footer-status")

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
        self.query_one("#chat-feed").display = False
        self.query_one("#chat-choices").display = False
        self.query_one("#chat-input-bar").display = False
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

        # Pregenerate common UI phrases and number words on mount so they're
        # cached before the first agent connects. This runs in the UI pregen
        # queue (low priority, 1 worker) to avoid competing with agent pregen.
        self._pregenerate_common_ui_texts()

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
            # Also reset TTS failure counters — PulseAudio being down
            # causes paplay failures that get counted as API failures,
            # so the TTS engine thinks the API is broken when really
            # it was just the audio output that was unreachable.
            try:
                self._tts.reset_failure_counters()
            except Exception:
                pass
            try:
                self._tts.play_chime("success")
            except Exception:
                pass
            try:
                _log.info("PulseAudio recovered")
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
        cooldown = 15.0  # Must be < health check interval (30s) to avoid race
        if self._config:
            max_attempts = self._config.pulse_max_reconnect_attempts
            cooldown = self._config.pulse_reconnect_cooldown

        now = time.time()

        # Check if we've exceeded max attempts — auto-reset after 5× cooldown
        # so reconnection keeps retrying periodically even after exhaustion
        if self._pulse_reconnect_attempts >= max_attempts:
            backoff = cooldown * 5
            if now - self._pulse_last_reconnect >= backoff:
                self._pulse_reconnect_attempts = 0
                try:
                    _log.info("PulseAudio reconnect attempts reset after backoff")
                except Exception:
                    pass
            else:
                return

        # Check cooldown
        if now - self._pulse_last_reconnect < cooldown:
            return

        self._pulse_reconnect_attempts += 1
        self._pulse_last_reconnect = now
        attempt = self._pulse_reconnect_attempts

        try:
            _log.warning(
                "PulseAudio reconnect attempt %d/%d", attempt, max_attempts,
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
            # Reset TTS failure counters — PulseAudio outage causes
            # paplay failures that get misattributed as API failures
            try:
                self._tts.reset_failure_counters()
            except Exception:
                pass
            try:
                self._tts.play_chime("success")
            except Exception:
                pass
            try:
                _log.info(
                    "PulseAudio reconnected successfully",
                    extra={"context": log_context(diagnostics=diagnostic_info)} if diagnostic_info else {},
                )
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
                _log.warning(
                    "PulseAudio reconnect failed (%d attempts remaining)",
                    remaining,
                    extra={"context": log_context(diagnostics=diagnostic_info)} if diagnostic_info else {},
                )
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
        _log.error(
            "PulseAudio auto-reconnect exhausted",
            extra={"context": log_context(
                recovery_steps=steps,
                diagnostics=diagnostic_info or "",
            )},
        )

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
                # Log to activity feed so chat view shows the ambient update
                session.log_activity("ambient", phrase, kind="ambient")
                self._notify_chat_feed_update(session)
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
                # Log to activity feed so chat view shows the ambient update
                session.log_activity("ambient", msg[:120], kind="ambient")
                self._notify_chat_feed_update(session)

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
                        health_icon = f"  [{s['success']}]●+{inbox_count - 1}[/{s['success']}]"
                    else:
                        health_icon = f"  [{s['success']}]●[/{s['success']}]"
                elif health == "warning":
                    health_icon = f"  [{s['warning']}]![/{s['warning']}]"
                elif health == "unresponsive":
                    health_icon = f"  [{s['error']}]✗[/{s['error']}]"
                else:
                    # Connected but idle (no active choices, healthy)
                    health_icon = f"  [{s['fg_dim']}]●[/{s['fg_dim']}]"
                # Streak fire
                streak = session.streak_minutes
                streak_text = ""
                if streak >= 3:
                    fires = min(streak // 5, 5)  # max 5 flames
                    streak_text = f" {'🔥' * max(1, fires)}{streak}m"
                lhs = f"[bold {s['accent']}]{name}[/bold {s['accent']}]{health_icon}{streak_text}"
            else:
                lhs = f"[bold {s['accent']}]io-mcp[/bold {s['accent']}]"
        else:
            lhs = self.manager.tab_bar_text(
                accent=s['accent'],
                success=s['success'],
                warning=s['warning'],
                error=s['error'],
                fg_dim=s['fg_dim'],
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

        # Update footer status line
        self._update_footer_status()

    def _update_footer_status(self) -> None:
        """Update the bottom status line with session context and keyboard hints."""
        try:
            footer = self.query_one("#footer-status", Static)
        except Exception:
            return

        session = self._focused()
        s = self._cs
        kl = getattr(self, '_key_labels', {})

        if session is None:
            footer.update(f"[{s['fg_dim']}]? help[/{s['fg_dim']}]")
            return

        parts = []

        # Session name
        parts.append(f"[{s['accent']}]{session.name}[/{s['accent']}]")

        # Tool call count
        if session.tool_call_count > 0:
            parts.append(f"[{s['fg_dim']}]{session.tool_call_count} calls[/{s['fg_dim']}]")

        # Pending inbox items
        pending = sum(1 for item in session.inbox if not item.done)
        if pending > 0:
            parts.append(f"[{s['warning']}]{pending} pending[/{s['warning']}]")

        # Pending messages
        if session.pending_messages:
            parts.append(f"[{s['purple']}]{len(session.pending_messages)} msg[/{s['purple']}]")

        # Agent working indicator — show latest status if recent (< 30s)
        _typing_text = self._get_agent_typing_status(session)
        if _typing_text:
            parts.append(f"[italic {s['fg_dim']}]{_typing_text}[/italic {s['fg_dim']}]")

        # Context-aware keyboard shortcut hints
        dim = s['fg_dim']
        if self._in_settings:
            # Settings mode hints
            _down = kl.get("down", "j")
            _up = kl.get("up", "k")
            _sel = kl.get("select", "Enter")
            _stg = kl.get("settings", "s")
            hints = f"{_down}/{_up}=scroll  {_sel}=select  {_stg}=back"
        elif session.active and session.choices:
            # Choices visible — navigation and selection hints
            _down = kl.get("down", "j")
            _up = kl.get("up", "k")
            _sel = kl.get("select", "Enter")
            _dis = kl.get("dismiss", "d")
            _undo = kl.get("undo", "u")
            hints = f"{_down}/{_up}=scroll  {_sel}=select  1-9=pick  {_dis}=dismiss  {_undo}=undo"
        elif self._chat_view_active:
            # Chat/feed view — show close hint and utility shortcuts
            _msg = kl.get("message", "m")
            _stg = kl.get("settings", "s")
            hints = f"g=close  {_msg}=message  {_stg}=settings"
        else:
            # Waiting — agent is working, show utility shortcuts
            _msg = kl.get("message", "m")
            _stg = kl.get("settings", "s")
            _pane = kl.get("pane", "v")
            hints = f"{_msg}=message  {_stg}=settings  {_pane}=pane  g=feed"
        parts.append(f"[{dim}]{hints}[/{dim}]")

        footer.update("  ".join(parts))

    def _get_agent_typing_status(self, session) -> str:
        """Return a transient typing/status string if the agent reported status recently.

        Checks the session's activity_log for the most recent 'status' entry.
        If it's less than 30 seconds old and the session isn't actively presenting
        choices, returns a truncated status string (e.g. "⟳ reading config").
        Returns empty string otherwise.
        """
        import time as _time

        if session.active:
            # Agent is presenting choices — no typing indicator needed
            return ""

        # Walk backward through activity log to find the last status entry
        for entry in reversed(session.activity_log):
            if entry.get("kind") == "status":
                age = _time.time() - entry["timestamp"]
                if age < 30:
                    detail = entry.get("detail", "")
                    if detail:
                        # Truncate long status text
                        if len(detail) > 40:
                            detail = detail[:37] + "…"
                        return f"⟳ {detail}"
                # Only check the most recent status entry
                break

        return ""

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

    def _dismiss_active_item(self) -> None:
        """Dismiss the active inbox item without sending a response to the agent.

        Marks the item as done with a _dismissed result. If the agent thread
        is alive, present_choices will return a cancelled-style result. If the
        agent is dead (restarted/crashed), this simply cleans up the stale item.

        Also handles stale items from dead/removed sessions: if the focused
        session no longer exists in the manager, or the inbox item's owner
        thread is dead, the item is directly cancelled and removed.

        Triggered by the "Dismiss" extra option or the 'd' keyboard shortcut.
        """
        session = self._focused()
        if not session:
            return

        item = getattr(session, '_active_inbox_item', None)
        if item and not item.done:
            # Mark done with a dismissed result — event.set() unblocks
            # any waiting thread (live agent) or is a no-op (dead agent)
            item.result = {"selected": "_dismissed", "summary": "Dismissed by user"}
            item.done = True
            item.event.set()
            session.drain_kick.set()

            # Move to done list if it's still in the inbox queue
            if item in session.inbox:
                session.inbox.remove(item)
                session._append_done(item)
                session._inbox_generation += 1

            # Clear active state
            session._active_inbox_item = None
            session.active = False
            session.preamble = ""
            session.choices = []

            self._speak_ui("Dismissed")
            self._safe_call(self._update_inbox_list)
            self._safe_call(self._update_tab_bar)

            # Suppress the waiting-state TTS announcement — dismiss already spoke
            session._waiting_announced = True

            # Auto-advance to next pending item or show waiting view
            self._safe_call(self._show_next_or_waiting)
        elif session.active or any(not it.done for it in session.inbox):
            # Session is marked active but the _active_inbox_item is missing
            # or already done, OR session has stale pending inbox items from
            # dead/removed agents.  Clean up all pending items and reset state.
            _resolve_pending_inbox(session)
            session._active_inbox_item = None
            session.active = False
            session.preamble = ""
            session.choices = []

            self._speak_ui("Dismissed stale item")
            self._safe_call(self._update_inbox_list)
            self._safe_call(self._update_tab_bar)

            # Suppress the waiting-state TTS announcement — dismiss already spoke
            session._waiting_announced = True

            self._safe_call(self._show_next_or_waiting)
        else:
            self._speak_ui("Nothing to dismiss")

    def _show_next_or_waiting(self) -> None:
        """After dismissing, show the next pending item or the waiting view."""
        session = self._focused()
        if session:
            # Check if there's another pending choice item
            front = session.peek_inbox()
            if front and front.kind == "choices" and not front.done:
                # Activate the next item
                session.preamble = front.preamble
                session.choices = list(front.choices)
                session.active = True
                session._active_inbox_item = front
                from .widgets import EXTRA_OPTIONS
                session.extras_count = len(EXTRA_OPTIONS)
                session.all_items = list(EXTRA_OPTIONS) + session.choices
                self._show_choices()
                return

            # No more pending items — show waiting state
            if self._chat_view_active:
                # Chat view: refresh feed to show dismissed item, hide choices panel
                self._chat_content_hash = ""  # Force rebuild
                self._refresh_chat_feed()
                try:
                    self.query_one("#chat-choices").display = False
                except Exception:
                    pass
                self._update_footer_status()
            else:
                self._show_waiting_with_shortcuts(session)

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
            _log.error("present_choices error: %s", err, exc_info=True)
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

        # Always update the unified inbox so new items appear immediately
        self._inbox_scroll_index = 0
        self._safe_call(self._update_inbox_list)

        # Play inbox chime if user is already viewing choices for this session
        if session.active and self._is_focused(session.session_id):
            self._tts.play_chime("inbox")

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

            # Show conversation UI — adapt to chat view vs normal view
            def _show_convo():
                if self._chat_view_active:
                    # Chat view: refresh feed to show new speech, hide choices panel
                    self._chat_content_hash = ""
                    self._refresh_chat_feed()
                    try:
                        self.query_one("#chat-choices").display = False
                    except Exception:
                        pass
                else:
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
            self._tts.speak(preamble)

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
        else:
            # Non-focused session got choices — auto-switch if the focused
            # session is idle (not actively showing choices). This gives
            # immediate attention to incoming choices without manual tab switching.
            focused = self._focused()
            if focused and not focused.active:
                import time as _time2
                _time2.sleep(0.3)  # Brief delay so inbox chime plays first
                self._safe_call(lambda: self._switch_to_session(session))
                self._safe_call(self._show_choices)

        # Update tab bar (session now has active choices indicator)
        self._safe_call(self._update_tab_bar)

        # Pregenerate TTS fragments in background.
        # Instead of pregenerating full strings like "1. Fix a bug. Debug and fix",
        # we pregenerate individual fragments: number words ("one", "two"),
        # the word "selected", and each label/summary separately.
        # This drastically reduces API calls since number words and "selected"
        # are reused across all choices.
        from io_mcp.tts import TTSEngine
        _num_words = TTSEngine._NUMBER_WORDS
        fragment_texts = set()
        for i, c in enumerate(choices):
            n = i + 1
            if n in _num_words:
                fragment_texts.add(_num_words[n])
            label = c.get('label', '')
            summary = c.get('summary', '')
            if label:
                fragment_texts.add(label)
            if summary:
                fragment_texts.add(summary)
        fragment_texts.add("selected")
        ui_speed = self._config.tts_speed_for("ui") if self._config else None
        self._pregenerate_worker(list(fragment_texts), speed_override=ui_speed)

        # Pregenerate extra option labels in separate UI queue
        # so they don't compete with agent choice pregeneration.
        # Include PRIMARY_EXTRAS, SECONDARY_EXTRAS, and MORE_OPTIONS_ITEM
        # so scrolling through expanded extras is also instant.
        from .widgets import PRIMARY_EXTRAS, SECONDARY_EXTRAS, MORE_OPTIONS_ITEM
        ui_texts = set()
        for e in list(PRIMARY_EXTRAS) + list(SECONDARY_EXTRAS) + [MORE_OPTIONS_ITEM]:
            label = e.get('label', '')
            summary = e.get('summary', '')
            if label:
                ui_texts.add(label)
            if summary:
                ui_texts.add(summary)
        if ui_texts:
            self._pregenerate_ui_worker(list(ui_texts))

        if is_fg:
            # Audio + haptic cue for new choices
            self._tts.play_chime("choices")
            self._vibrate_pattern("pulse")

            # Speak the full intro (preamble + option titles) sequentially.
            # The speech lock in TTSEngine ensures this queues behind any
            # in-progress speech without interrupting it.
            preamble_speed = self._config.tts_speed_for("preamble") if self._config else None
            self._tts.speak(full_intro, speed_override=preamble_speed)

            session.intro_speaking = False
            # Read each option with full summary — breaks if user scrolls
            if session.active and not session.selection:
                session.reading_options = True
                for i, text in enumerate(numbered_full_all):
                    if not session.reading_options or not session.active:
                        break
                    if not self.manager.get(session.session_id):
                        break  # session was removed
                    # Skip silent options in the readout
                    if i < len(choices) and choices[i].get('_silent', False):
                        continue
                    self._tts.speak(text)
                session.reading_options = False

            # Try playing any background queued speech
            self._try_play_background_queue()
        else:
            # Background: queue intro for later, read abbreviated version
            session.intro_speaking = False
            entry = SpeechEntry(text=full_intro)
            session.unplayed_speech.append(entry)
            session.append_speech(SpeechEntry(text=f"[choices] {preamble}"))

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

        # Reset waiting-state announcement flag so it fires again next
        # time the session enters the waiting state.
        session._waiting_announced = False

        _log.info("_show_choices: entering", extra={"context": {
            "chat_view_active": self._chat_view_active,
            "session_active": session.active,
            "n_choices": len(session.choices) if session.choices else 0,
            "session": session.name,
        }})

        # Refresh chat feed if active (so new choices appear in the timeline)
        if self._chat_view_active:
            self._chat_content_hash = ""  # Force rebuild
            self._refresh_chat_feed()
            # Show scrollable choices list at the bottom for j/k/scroll navigation
            if session.active and session.choices:
                _log.info("_show_choices: populating chat-choices",
                          extra={"context": {"n_choices": len(session.choices)}})
                self._populate_chat_choices_list(session)
            else:
                try:
                    self.query_one("#chat-choices").display = False
                except Exception:
                    pass
            self._update_footer_status()
            return

        # Don't overwrite the UI if user is composing a message or typing
        if self._message_mode or (session and session.input_mode):
            # Choices are stored on the session; they'll be shown after input is done
            # Play inbox chime + brief announcement so screen-off user knows what happened
            self._tts.play_chime("inbox")
            if session:
                self._speak_ui(f"Choices from {session.name}")
            return

        preamble_widget = self.query_one("#preamble", Label)
        preamble_widget.update(session.preamble)
        preamble_widget.display = True

        self.query_one("#status").display = False

        # Show the main content container
        self._ensure_main_content_visible(show_inbox=True)

        list_view = self.query_one("#choices", ListView)

        # Guard against MountError during TUI restarts — if the ListView
        # isn't mounted yet, skip this update. The choices are stored on
        # the session and will be shown when _show_choices is called again.
        if not list_view.is_mounted:
            return

        list_view.clear()

        # Build the extras portion based on expand/collapse state
        if self._extras_expanded:
            # Expanded: show all extras (secondary + primary)
            visible_extras = list(SECONDARY_EXTRAS) + list(PRIMARY_EXTRAS)
        else:
            # Collapsed: just "More options ›" + primary extras (Record response)
            visible_extras = [MORE_OPTIONS_ITEM] + list(PRIMARY_EXTRAS)

        # Wrap all appends in a try/except to catch MountError — the
        # is_mounted check above can race with a concurrent TUI restart
        # that unmounts the ListView between our check and the append.
        try:
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
        except Exception:
            # MountError or similar — the ListView was unmounted mid-append.
            # Choices are stored on the session and will be re-shown when
            # _show_choices is called again after the TUI restart completes.
            return

        list_view.display = True
        # Restore scroll position or default to first real choice
        n_extras = len(visible_extras)
        if session.scroll_index > 0 and session.scroll_index < len(list_view.children):
            list_view.index = session.scroll_index
        elif len(list_view.children) > n_extras:
            list_view.index = n_extras  # first real choice
        else:
            list_view.index = 0

        # Focus the choices pane (right side) — this is the actionable content.
        # The inbox pane (left) is for context/navigation only.
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

    def _populate_chat_choices_list(self, session) -> None:
        """Populate the standalone #chat-choices ListView for chat view.

        Shows choices in a scrollable list at the bottom of the screen,
        separate from the chat feed. Supports j/k scroll and Enter to select.
        The preamble (agent question/context) is prepended as a non-selectable
        header item above the choices.
        """
        try:
            list_view = self.query_one("#chat-choices", ListView)
        except Exception:
            return
        list_view.clear()
        # Prepend preamble as a non-selectable header
        preamble_text = session.preamble or ""
        if preamble_text:
            list_view.append(PreambleItem(preamble_text))
        choices = session.choices or []
        for i, c in enumerate(choices):
            label = c.get('label', '')
            summary = c.get('summary', '')
            list_view.append(ChoiceItem(label, summary, index=i+1, display_index=i))
        # Add primary extras (voice input, etc.) with negative indices
        # so on_list_selected routes them to _handle_extra_select
        di = len(choices)
        extras_list = list(PRIMARY_EXTRAS)

        # Add "More options" toggle + secondary extras in chat view too,
        # so all features are accessible via scroll+enter (not just keyboard shortcuts)
        if getattr(self, '_chat_extras_expanded', False):
            extras_list = list(SECONDARY_EXTRAS) + extras_list
        else:
            extras_list = [MORE_OPTIONS_ITEM] + extras_list

        for ei, e in enumerate(extras_list):
            list_view.append(ChoiceItem(
                e.get('label', ''), e.get('summary', ''),
                index=-(len(extras_list) - 1 - ei), display_index=di,
            ))
            di += 1
        list_view.display = True
        # Start on first selectable item (skip preamble header)
        list_view.index = 1 if preamble_text else 0
        list_view.focus()
        _log.info("_populate_chat_choices_list: populated", extra={"context": {
            "n_choices": len(choices),
            "n_extras": di - len(choices),
            "has_preamble": bool(preamble_text),
            "display": True,
        }})

    def _show_waiting(self, label: str) -> None:
        """Show waiting state after selection, returning to unified inbox.

        After resolving a choice, focus returns to the unified inbox list
        (left pane) with the cursor at the top so the user sees the newest
        items immediately. The right pane shows a simple waiting state.
        If there are more pending items, the next one will auto-present
        via the inbox drain loop.
        """
        _log.info("_show_waiting: entering", extra={"context": {
            "label": label,
            "chat_view_active": self._chat_view_active,
        }})
        # Chat view: just refresh the feed and hide choices panel
        if self._chat_view_active:
            self._chat_content_hash = ""  # Force rebuild
            self._refresh_chat_feed()
            try:
                self.query_one("#main-content").display = False
                self.query_one("#chat-choices").display = False
                self.query_one("#preamble").display = False
                self.query_one("#dwell-bar").display = False
                self.query_one("#status").display = False
            except Exception:
                pass
            self._update_footer_status()
            return

        self.query_one("#preamble").display = False
        self.query_one("#dwell-bar").display = False
        status = self.query_one("#status", Label)
        session = self._focused()
        session_name = session.name if session else ""
        after_text = f"Selected: {label}" if self._demo else f"[{self._cs['success']}]*[/{self._cs['success']}] [{session_name}] {label} [dim](u=undo)[/dim]"
        status.update(after_text)
        status.display = True

        # Show waiting state with metadata in the right pane
        self._show_waiting_with_shortcuts(session)

        # Return to unified inbox — scroll to top so newest items are visible
        self._inbox_scroll_index = 0
        self._ensure_main_content_visible(show_inbox=True)

        # Focus the inbox list (left pane) so user can browse other items
        self._inbox_pane_focused = True
        if self._inbox_pane_visible():
            try:
                inbox_list = self.query_one("#inbox-list", ListView)
                inbox_list.focus()
            except Exception:
                pass

    def _show_idle(self) -> None:
        """Show idle state with inbox view."""
        _log.info("_show_idle: entering", extra={"context": {
            "chat_view_active": self._chat_view_active,
        }})
        self.query_one("#preamble").display = False
        self.query_one("#dwell-bar").display = False
        self.query_one("#speech-log").display = False
        status = self.query_one("#status", Label)
        session = self._focused()

        if session is None:
            status_text = "[dim]Ready -- demo mode[/dim]" if self._demo else "[dim]Waiting for agent...[/dim]"
            status.update(status_text)
            status.display = not self._chat_view_active
            if not self._chat_view_active:
                self.query_one("#main-content").display = False
            self._update_footer_status()
            # TTS guidance for screen-off users — let them know what keys are available
            if not self._demo:
                self._speak_ui("No agents connected. Press t to spawn, s for settings, q to quit.")
            return

        if session.tool_call_count > 0:
            status_text = f"[dim]{session.name} -- working...[/dim]"
        else:
            status_text = f"[{self._cs['accent']}]{session.name} connected[/{self._cs['accent']}]"
        status.update(status_text)
        # In chat view, don't show status — the feed provides context
        status.display = not self._chat_view_active

        # In chat view, rebuild feed for new session and hide choices panel
        if self._chat_view_active:
            self._chat_content_hash = ""  # Force rebuild
            self._chat_last_item_count = 0  # Reset for new session
            self._chat_base_fingerprint = ""  # Reset for new session
            self._refresh_chat_feed()
            try:
                self.query_one("#main-content").display = False
                self.query_one("#chat-choices").display = False
            except Exception:
                pass
            self._update_footer_status()
            return

        # Show inbox view with history
        self._ensure_main_content_visible(show_inbox=True)

        # Delegate to _show_waiting_with_shortcuts for a richer waiting state
        # that includes agent metadata, shortcuts, and pending messages
        self._show_waiting_with_shortcuts(session)

        self._update_footer_status()

    def _show_waiting_with_shortcuts(self, session) -> None:
        """Show a clean waiting state with essential keyboard shortcuts.

        Replaces the old activity feed. Shows the inbox view (left pane)
        with a minimal right pane containing status and shortcut hints.
        In chat view, this is a no-op — the chat feed provides context.
        """
        if self._chat_view_active:
            _log.info("_show_waiting_with_shortcuts: chat view, returning early")
            return
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

            # ── Mood ring (color based on agent activity) ─────
            mood = session.mood
            _mood_colors = {
                "idle":      s.get("blue", "#81a1c1"),
                "flowing":   s.get("success", "#a3be8c"),
                "busy":      s.get("warning", "#ebcb8b"),
                "thrashing": s.get("error", "#bf616a"),
                "speaking":  s.get("purple", "#b48ead"),
            }
            _mood_icons = {
                "idle": "◯", "flowing": "●", "busy": "◉",
                "thrashing": "⊛", "speaking": "♫",
            }
            mood_color = _mood_colors.get(mood, s['fg_dim'])
            mood_icon = _mood_icons.get(mood, "·")

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
                status_text = f"[{mood_color}]{mood_icon}[/{mood_color}] [{s['fg_dim']}]{mood} · last activity {ago}[/{s['fg_dim']}]"
            else:
                status_text = f"[{mood_color}]{mood_icon}[/{mood_color}] [{s['fg_dim']}]Waiting for agent...[/{s['fg_dim']}]"
            list_view.append(ChoiceItem(
                status_text, "",
                index=-999, display_index=di,
            ))
            di += 1

            # ── Agent metadata (cwd, hostname, tool stats) ────
            if session.registered:
                meta_parts = []
                if session.hostname:
                    meta_parts.append(session.hostname)
                if session.cwd:
                    # Abbreviate home directory and long paths
                    cwd = session.cwd
                    home = os.path.expanduser("~")
                    if cwd.startswith(home):
                        cwd = "~" + cwd[len(home):]
                    if len(cwd) > 50:
                        cwd = "…" + cwd[-49:]
                    meta_parts.append(cwd)
                if meta_parts:
                    meta_text = f"[{s['fg_dim']}]{'  ·  '.join(meta_parts)}[/{s['fg_dim']}]"
                    list_view.append(ChoiceItem(
                        meta_text, "",
                        index=-997, display_index=di,
                    ))
                    di += 1

                # Tool call stats line
                stats_parts = []
                if session.tool_call_count > 0:
                    stats_parts.append(f"{session.tool_call_count} tool calls")
                if session.last_tool_name:
                    stats_parts.append(f"last: {session.last_tool_name}")
                if session.tmux_pane:
                    stats_parts.append(f"pane {session.tmux_pane}")
                if stats_parts:
                    stats_text = f"[{s['fg_dim']}]{'  ·  '.join(stats_parts)}[/{s['fg_dim']}]"
                    list_view.append(ChoiceItem(
                        stats_text, "",
                        index=-996, display_index=di,
                    ))
                    di += 1

                # Custom metadata from agent_metadata dict
                if session.agent_metadata:
                    custom_parts = [f"{k}={v}" for k, v in session.agent_metadata.items()]
                    if custom_parts:
                        custom_text = f"[{s['fg_dim']}]{'  ·  '.join(custom_parts)}[/{s['fg_dim']}]"
                        list_view.append(ChoiceItem(
                            custom_text, "",
                            index=-995, display_index=di,
                        ))
                        di += 1

            # ── Activity sparkline (tool calls per minute) ──────
            if session.activity_log and len(session.activity_log) >= 3:
                import time as _time_mod
                now = _time_mod.time()
                # Bucket activity into 1-minute slots over last 10 minutes
                _blocks = " ▁▂▃▄▅▆▇█"
                buckets = [0] * 10
                for entry in session.activity_log:
                    age_mins = int((now - entry["timestamp"]) / 60)
                    if 0 <= age_mins < 10:
                        buckets[9 - age_mins] += 1  # newest on right
                max_val = max(buckets) if max(buckets) > 0 else 1
                sparkline = ""
                for b in buckets:
                    idx = min(int(b / max_val * 8), 8) if max_val > 0 else 0
                    sparkline += _blocks[idx]
                list_view.append(ChoiceItem(
                    f"[{s['accent']}]{sparkline}[/{s['accent']}] [{s['fg_dim']}]activity (10m)[/{s['fg_dim']}]", "",
                    index=-997, display_index=di,
                ))
                di += 1

            # ── Last selection (what the user chose) ──────────
            if session.selection:
                sel_text = session.selection.get("selected", "")[:60]
                if sel_text and sel_text not in ("_restart", "_cancelled", "_dismissed", "error"):
                    list_view.append(ChoiceItem(
                        f"[{s['fg_dim']}]Last: {sel_text}[/{s['fg_dim']}]", "",
                        index=-993, display_index=di,
                    ))
                    di += 1

            # ── Recent speech log (last 3 entries) ────────────
            recent_speech = session.speech_log[-3:] if session.speech_log else []
            if recent_speech:
                list_view.append(ChoiceItem(
                    f"[{s['fg_dim']}]─── Recent ───[/{s['fg_dim']}]", "",
                    index=-992, display_index=di,
                ))
                di += 1
                for entry in recent_speech:
                    text_preview = entry.text[:80] if entry.text else ""
                    list_view.append(ChoiceItem(
                        f"[{s['fg_dim']}]{text_preview}[/{s['fg_dim']}]", "",
                        index=-991, display_index=di,
                    ))
                    di += 1

            # ── Activity log (recent agent actions) ────────────
            activity = session.activity_log[-8:] if session.activity_log else []
            if activity:
                import time as _time_mod
                now = _time_mod.time()
                list_view.append(ChoiceItem(
                    f"[{s['fg_dim']}]─── Activity ───[/{s['fg_dim']}]", "",
                    index=-990, display_index=di,
                ))
                di += 1

                # Icons for different activity kinds
                _icons = {
                    "speech": "🔊",
                    "choices": "📋",
                    "settings": "⚙",
                    "status": "📡",
                    "tool": "⚡",
                }

                for entry in reversed(activity):
                    elapsed = now - entry["timestamp"]
                    if elapsed < 60:
                        ago = f"{int(elapsed)}s"
                    elif elapsed < 3600:
                        ago = f"{int(elapsed) // 60}m"
                    else:
                        ago = f"{int(elapsed) // 3600}h"

                    icon = _icons.get(entry["kind"], "·")
                    tool = entry["tool"]
                    detail = entry.get("detail", "")
                    if detail:
                        text = f"[{s['fg_dim']}]{icon} {ago:>4}  {tool}  {detail[:50]}[/{s['fg_dim']}]"
                    else:
                        text = f"[{s['fg_dim']}]{icon} {ago:>4}  {tool}[/{s['fg_dim']}]"

                    list_view.append(ChoiceItem(
                        text, "",
                        index=-989, display_index=di,
                    ))
                    di += 1

            # ── Session timeline bar ───────────────────────────
            if session.activity_log and len(session.activity_log) >= 2:
                import time as _time_mod2
                now2 = _time_mod2.time()
                first_ts = session.activity_log[0]["timestamp"]
                span = now2 - first_ts
                if span > 60:  # Only show if session is 1+ minutes old
                    bar_width = 30
                    bar = ["░"] * bar_width  # empty slots
                    _kind_chars = {
                        "speech": "█", "choices": "▓",
                        "tool": "▒", "settings": "░", "status": "▒",
                    }
                    for entry in session.activity_log:
                        pos = int((entry["timestamp"] - first_ts) / span * (bar_width - 1))
                        pos = min(pos, bar_width - 1)
                        ch = _kind_chars.get(entry["kind"], "▒")
                        # Higher priority overwrites
                        if bar[pos] == "░" or ch in ("█", "▓"):
                            bar[pos] = ch
                    bar_str = "".join(bar)
                    mins = int(span / 60)
                    list_view.append(ChoiceItem(
                        f"[{s['accent']}]{bar_str}[/{s['accent']}] [{s['fg_dim']}]{mins}m session[/{s['fg_dim']}]", "",
                        index=-988, display_index=di,
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

            # ── One-time TTS announcement on entering waiting state ──
            # Speaks shortcut hints so screen-off users know what keys
            # are available. Only fires once per waiting-state entry;
            # reset when choices are next presented.
            if not session._waiting_announced:
                session._waiting_announced = True
                kl = getattr(self, '_key_labels', {})
                msg_key = kl.get("message", "m")
                settings_key = kl.get("settings", "s")
                dismiss_key = kl.get("dismiss", "d")
                agent_name = session.name or "Agent"
                self._speak_ui(
                    f"{agent_name} is working. "
                    f"Press {msg_key} to message, "
                    f"{settings_key} for settings, "
                    f"{dismiss_key} to dismiss."
                )
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
        In chat view, the inbox is never shown — skip entirely.
        """
        # Chat view: inbox is hidden, skip all work
        if self._chat_view_active:
            _log.info("_update_inbox_list: chat view, returning early")
            return
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

            # Always check collapsed state first (user toggle takes priority)
            if self._inbox_collapsed:
                inbox_list.display = False
                return

            if gen == self._inbox_last_generation:
                if self._inbox_scroll_index < len(inbox_list.children):
                    inbox_list.index = self._inbox_scroll_index
                return
            self._inbox_last_generation = gen

            inbox_list.clear()

            # Collect choice items from all sessions (skip speech-only items
            # which process automatically and add noise to the inbox)
            all_pending: list[tuple[InboxItem, Session]] = []
            all_done: list[tuple[InboxItem, Session]] = []
            any_registered = False

            for sess in sessions:
                if sess.registered:
                    any_registered = True
                for item in sess.inbox:
                    if not item.done and item.kind == "choices":
                        all_pending.append((item, sess))
                for item in sess.inbox_done:
                    all_done.append((item, sess))

            # Deduplicate done items per session — only show choice items
            # (speech-only done items are noise: "Running tests", "All passed", etc.)
            done_deduped: list[tuple[InboxItem, Session]] = []
            for sess in sessions:
                sess_done = [item for item in sess.inbox_done if item.kind == "choices"]
                deduped = self._dedup_done_items(sess_done)
                for item in deduped[-5:]:  # Last 5 done per session
                    done_deduped.append((item, sess))

            total = len(all_pending) + len(done_deduped)
            multi_agent = self.manager.count() > 1

            if total == 0 and not any_registered:
                inbox_list.display = False
                return

            inbox_list.display = True

            s = getattr(self, '_cs', {})
            accent_color = s.get('accent', '#88c0d0')

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
            _log.debug("_update_inbox_list failed", exc_info=True)

    def _get_inbox_item_at_index(self, idx: int) -> Optional[InboxItem]:
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
            _log.debug("Failed to emit speech event to frontend API", exc_info=True)

        # Log the speech
        entry = SpeechEntry(text=text, priority=priority)
        session.append_speech(entry)

        # Update speech log UI if this is the focused session
        if self._is_focused(session.session_id):
            self._safe_call(self._update_speech_log)

        if self._is_focused(session.session_id):
            # Foreground: play immediately
            voice_ov = getattr(session, 'voice_override', None)
            model_ov = getattr(session, 'model_override', None)
            # Per-call emotion > session override > config default
            emotion_ov = emotion if emotion else getattr(session, 'emotion_override', None)

            # No interruption — speech queues sequentially via the
            # TTSEngine speech lock. Urgent items queue at front of
            # the inbox but don't kill current playback.
            if block:
                speak_speed = self._config.tts_speed_for("speak") if self._config else None
                self._tts.speak(text, voice_override=voice_ov,
                                emotion_override=emotion_ov,
                                model_override=model_ov,
                                speed_override=speak_speed)
            else:
                async_speed = self._config.tts_speed_for("speakAsync") if self._config else None
                self._tts.speak_async(text, voice_override=voice_ov,
                                     emotion_override=emotion_ov,
                                     model_override=model_ov,
                                     speed_override=async_speed)
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

        # Immediately refresh chat feed so new items appear without
        # waiting for the 3-second timer
        self._safe_call(lambda: self._notify_chat_feed_update(session))

        # Start a drain worker for this session if speech items need processing
        try:
            self._drain_session_inbox_worker(session)
        except RuntimeError:
            # App is not running (e.g. during TUI restart) — skip drain
            pass

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

        Error in processing one item does not prevent processing the next —
        failed items are marked done with an error result so the loop continues.
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
            if front.processing:
                # Another drain worker is already handling this item —
                # don't double-process (which would kill its TTS playback)
                break

            # ── Process speech item ──
            front.processing = True
            try:
                self._activate_speech_item(session, front)
            except Exception:
                _log.error(
                    "Error processing speech inbox item: %s",
                    (front.text or front.preamble)[:80],
                    exc_info=True,
                )
                # Force-resolve the failed item so we can move to the next one
                if not front.done:
                    front.result = {"selected": "_speech_done", "summary": "error"}
                    front.done = True
                    front.event.set()
                    try:
                        session._append_done(session.inbox.popleft())
                    except IndexError:
                        pass
                    session.drain_kick.set()

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
        session.append_speech(entry)

        # Emit event for remote frontends
        try:
            frontend_api.emit_speech_requested(
                session.session_id, text, blocking=True, priority=priority)
        except Exception:
            _log.debug("Failed to emit speech event for inbox speech item", exc_info=True)

        # Update inbox UI to show this item as active
        session._active_inbox_item = item
        self._safe_call(self._update_inbox_list)

        # Show speech text in right pane if this is the focused session
        if self._is_focused(session.session_id):
            self._safe_call(lambda: self._show_speech_item(text))

        # Play TTS — only for the focused session to prevent agents
        # talking over each other (all agents share one TTSEngine/paplay)
        voice_ov = getattr(session, 'voice_override', None)
        model_ov = getattr(session, 'model_override', None)
        emotion_ov = getattr(session, 'emotion_override', None)

        is_focused = self._is_focused(session.session_id)

        if is_focused:
            # No interruption — speech queues sequentially via the
            # TTSEngine speech lock.
            self._tts.speak(text, voice_override=voice_ov,
                            emotion_override=emotion_ov,
                            model_override=model_ov)
        else:
            # Non-focused session: queue for later playback, don't play now
            session.unplayed_speech.append(SpeechEntry(text=text, priority=priority))

        # Auto-resolve: mark done and kick drain
        item.result = {"selected": "_speech_done", "summary": text[:100]}
        item.done = True
        item.event.set()
        session._append_done(session.inbox.popleft())
        session.drain_kick.set()
        self._safe_call(self._update_inbox_list)
        self._safe_call(self._update_tab_bar)

        # Immediately refresh chat feed so the new speech bubble appears
        # without waiting for the 3-second timer
        self._safe_call(lambda: self._notify_chat_feed_update(session))

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
            _log.debug("_show_speech_item failed", exc_info=True)

    def _try_play_background_queue(self) -> None:
        """Try to play queued background speech if foreground is idle."""
        # Find any session with unplayed speech
        for session in self.manager.all_sessions():
            if session.session_id == self.manager.active_session_id:
                continue  # skip foreground
            while session.unplayed_speech:
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
            self._tts.speak(entry.text)

        # Then read prompt + options
        if session.active:
            numbered_labels = [
                f"{i+1}. {c.get('label', '')}" for i, c in enumerate(session.choices)
            ]
            titles_readout = " ".join(numbered_labels)
            full_intro = f"{session.preamble} Your options are: {titles_readout}"
            self._tts.speak(full_intro)

            # Read all options
            session.reading_options = True
            for i, c in enumerate(session.choices):
                if not session.reading_options or not session.active:
                    break
                s = c.get('summary', '')
                text = f"{i+1}. {c.get('label', '')}. {s}" if s else f"{i+1}. {c.get('label', '')}"
                self._tts.speak(text)
            session.reading_options = False

    @work(thread=True, exit_on_error=False, group="play_inbox")
    def _play_inbox_only_worker(self, session: Session) -> None:
        """Worker: play unplayed speech entries in background."""
        while session.unplayed_speech:
            entry = session.unplayed_speech.pop(0)
            entry.played = True
            self._tts.speak(entry.text)

    def _show_session_waiting(self, session: Session) -> None:
        """Show waiting state for a specific session."""
        # Chat view handles its own waiting state
        if self._chat_view_active:
            return
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

        # Delegate to _show_waiting_with_shortcuts for a richer waiting state
        # that includes agent metadata, shortcuts, and pending messages
        self._show_waiting_with_shortcuts(session)

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
        # Skip confirmation if config allows it
        if self._config and self._config.always_allow_restart_tui:
            self._speak_ui("Restarting TUI in 2 seconds")
            self._do_tui_restart_worker()
            return

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
        """Redraw the choice list with checkbox state.

        Updates existing ChoiceItem widgets in-place when possible to avoid
        ListView scroll-position reset.  Falls back to a full clear+rebuild
        only on the initial call (when the list is empty or has the wrong
        number of children).

        In chat view, renders into ``#chat-choices`` instead of ``#choices``
        and uses a PreambleItem header (since ``#preamble`` is inside the
        hidden ``#main-content``).
        """
        session = self._focused()
        if not session:
            return

        s = self._cs
        checked_count = sum(self._multi_select_checked)
        total = len(session.choices)

        preamble_text = (
            f"[bold {s['purple']}]Multi-select[/bold {s['purple']}] — "
            f"[{s['success']}]{checked_count}[/{s['success']}]/{total} selected "
            f"[dim](enter=toggle, scroll to confirm)[/dim]"
        )

        # In chat view use #chat-choices; otherwise use #choices + #preamble
        has_preamble_item = self._chat_view_active
        if self._chat_view_active:
            list_view = self.query_one("#chat-choices", ListView)
        else:
            preamble_widget = self.query_one("#preamble", Label)
            preamble_widget.update(preamble_text)
            preamble_widget.display = True
            list_view = self.query_one("#choices", ListView)

        # Expected item count: 1 (select-all) + total (choices) + 3 (confirm/team/cancel)
        # In chat view, add 1 more for the PreambleItem header
        expected_count = (1 if has_preamble_item else 0) + 1 + total + 3

        # --- Build labels/summaries for every row ---
        all_selected = all(self._multi_select_checked) if self._multi_select_checked else False

        # Row 0: select-all / deselect-all toggle
        toggle_label = (
            f"[{s['accent']}][ ] Deselect all[/{s['accent']}]"
            if all_selected
            else f"[{s['accent']}][*] Select all[/{s['accent']}]"
        )
        toggle_summary = f"{'Deselect' if all_selected else 'Select'} all {total} items"

        # Rows 1..total: checkable choices
        choice_labels: list[str] = []
        choice_summaries: list[str] = []
        for i, c in enumerate(session.choices):
            is_checked = i < len(self._multi_select_checked) and self._multi_select_checked[i]
            if is_checked:
                check = f"[{s['success']}][x][/{s['success']}]"
            else:
                check = f"[{s['fg_dim']}][ ][/{s['fg_dim']}]"
            num = str(i + 1)
            pad = " " * (2 - len(num))
            choice_labels.append(f"{pad}{num}. {check}  {c.get('label', '')}")
            choice_summaries.append(c.get("summary", ""))

        # Confirm summary
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

        confirm_label = f"[bold {s['success']}]✅ Confirm ({checked_count})[/bold {s['success']}]"
        team_label = f"[bold {s['accent']}]🚀 Team mode ({checked_count})[/bold {s['accent']}]"
        team_summary = f"Delegate {checked_count} task{'s' if checked_count != 1 else ''} to parallel sub-agents"

        # --- In-place update path (same number of children) ---
        if len(list_view.children) == expected_count:
            items: list = list(list_view.children)  # ChoiceItem (+ PreambleItem)

            offset = 0
            # Chat view: first child is a PreambleItem header
            if has_preamble_item:
                try:
                    items[0].query_one(".preamble-text", Label).update(preamble_text)
                except Exception:
                    pass
                offset = 1

            # Row: select-all toggle
            items[offset].update_content(toggle_label, toggle_summary)

            # Rows: choices
            for i in range(total):
                items[offset + 1 + i].update_content(choice_labels[i], choice_summaries[i])

            # Confirm / Team mode / Cancel
            items[offset + total + 1].update_content(confirm_label, selected_summary)
            items[offset + total + 2].update_content(team_label, team_summary)
            # Cancel row text is static — no update needed

            list_view.display = True
            list_view.focus()
            return

        # --- Full rebuild path (initial call or structural change) ---
        current_idx = list_view.index or 0

        list_view.clear()

        # Chat view: prepend preamble header as a PreambleItem
        if has_preamble_item:
            list_view.append(PreambleItem(preamble_text))

        list_view.append(ChoiceItem(
            toggle_label, toggle_summary,
            index=-99, display_index=0,
        ))

        for i in range(total):
            list_view.append(ChoiceItem(
                choice_labels[i], choice_summaries[i],
                index=i + 1, display_index=i + 1,
            ))

        confirm_offset = total + 1
        list_view.append(ChoiceItem(
            confirm_label, selected_summary,
            index=total + 1, display_index=confirm_offset,
        ))
        list_view.append(ChoiceItem(
            team_label, team_summary,
            index=total + 2, display_index=confirm_offset + 1,
        ))
        list_view.append(ChoiceItem(
            f"[dim]Cancel[/dim]", "Return to choices",
            index=total + 3, display_index=confirm_offset + 2,
        ))

        list_view.display = True
        # Restore position or default to first selectable item
        first_selectable = 1 if has_preamble_item else 0
        if current_idx >= first_selectable and current_idx < len(list_view.children):
            list_view.index = current_idx
        else:
            list_view.index = first_selectable
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

        # In chat view, skip inbox logic — go straight to tab switching
        if not self._chat_view_active:
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
        In chat view, inbox logic is skipped — goes straight to tab switching.
        """
        session = self._focused()
        if session and (session.input_mode or session.voice_recording):
            return

        # In chat view, skip inbox logic — go straight to tab switching
        if not self._chat_view_active:
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

    @_safe_action
    def action_dismiss_item(self) -> None:
        """Dismiss the active inbox item (keyboard shortcut for 'Dismiss' extra)."""
        session = self._focused()
        if not session:
            return
        # Block during text input and voice recording
        if session.input_mode or session.voice_recording:
            return
        # Works when there's an active choice item, or when there are stale
        # pending inbox items that need cleanup (e.g. from dead sessions)
        has_pending = any(not item.done for item in session.inbox)
        if not session.active and not has_pending:
            self._speak_ui("Nothing to dismiss")
            return
        self._dismiss_active_item()

    def action_toggle_sidebar(self) -> None:
        """Toggle the inbox sidebar collapsed/expanded. Persists across restarts."""
        session = self._focused()
        if session and (session.input_mode or session.voice_recording):
            return
        if self._inbox_collapsed:
            self._inbox_collapsed = False
            ui_state.set("inbox_collapsed", False)
            self._update_inbox_list()
            self._speak_ui("Inbox expanded")
        else:
            self._inbox_collapsed = True
            ui_state.set("inbox_collapsed", True)
            try:
                self.query_one("#inbox-list").display = False
            except Exception:
                pass
            self._inbox_pane_focused = False
            self.query_one("#choices", ListView).focus()
            self._speak_ui("Inbox collapsed")

    # ─── Session lifecycle ─────────────────────────────────────────

    def _pick_random_voice(self, voice_rot: list[dict]) -> dict:
        """Pick a random voice from rotation that isn't currently in use.

        Prefers voices not assigned to any active session. If all voices
        are in use, picks the one assigned to the fewest sessions (LRU-ish).
        """
        in_use = self.manager.in_use_voices()
        # Find candidates not currently in use (check both preset name and raw voice)
        available = [v for v in voice_rot
                     if v.get("preset", v.get("voice")) not in in_use
                     and v.get("voice") not in in_use]
        if available:
            return random.choice(available)
        # All voices in use — just pick randomly from the full list
        return random.choice(voice_rot)

    def _pick_random_emotion(self, emotion_rot: list[str]) -> str:
        """Pick a random emotion from rotation that isn't currently in use.

        Prefers emotions not assigned to any active session. If all emotions
        are in use, picks randomly from the full list.
        """
        in_use = self.manager.in_use_emotions()
        available = [e for e in emotion_rot if e not in in_use]
        if available:
            return random.choice(available)
        return random.choice(emotion_rot)

    def on_session_created(self, session: Session) -> None:
        """Called when a new session is created (from MCP thread).

        Assigns voice/emotion from rotation lists if configured.
        Supports random assignment (default) or legacy sequential mode.
        """
        # Audio cue for new agent connection
        self._tts.play_chime("connect")

        # Assign voice/emotion rotation
        if self._config:
            voice_rot = self._config.tts_voice_rotation
            emotion_rot = self._config.tts_emotion_rotation
            use_random = self._config.tts_random_rotation

            if voice_rot:
                if use_random:
                    entry = self._pick_random_voice(voice_rot)
                else:
                    session_idx = self.manager.count() - 1  # 0-based
                    entry = voice_rot[session_idx % len(voice_rot)]
                # Store preset name as voice_override (tts_cli_args resolves it)
                session.voice_override = entry.get("preset", entry.get("voice"))
                if entry.get("model"):
                    session.model_override = entry["model"]
            if emotion_rot:
                if use_random:
                    session.emotion_override = self._pick_random_emotion(emotion_rot)
                else:
                    session_idx = self.manager.count() - 1
                    session.emotion_override = emotion_rot[session_idx % len(emotion_rot)]

        try:
            self._call_on_main_thread(self._update_tab_bar)
        except Exception:
            _log.debug("on_session_created: update_tab_bar failed", exc_info=True)

        # Auto-activate chat view on first session connection.
        if not self._chat_view_active:
            def _activate_chat():
                if self._chat_view_active:
                    _log.info("_activate_chat: already active, skipping")
                    return
                _log.info("_activate_chat: activating chat view")
                self._chat_view_active = True
                # Determine unified mode based on session count
                all_sessions = list(self.manager.all_sessions()) if hasattr(self, 'manager') else []
                self._chat_unified = len(all_sessions) > 1
                try:
                    self.query_one("#chat-feed").display = True
                    self.query_one("#chat-input-bar").display = True
                    self.query_one("#main-content").display = False
                    self.query_one("#inbox-list").display = False
                    self.query_one("#preamble").display = False
                    self.query_one("#status").display = False
                    self.query_one("#speech-log").display = False
                    self.query_one("#agent-activity").display = False
                    self.query_one("#pane-view").display = False
                except Exception:
                    pass
                # Build feed immediately so it's not empty for 3 seconds
                focused = self._focused()
                if focused:
                    if self._chat_unified:
                        self._build_chat_feed(focused, sessions=all_sessions)
                    else:
                        self._build_chat_feed(focused)
                # Start auto-refresh (stop any existing timer first)
                if hasattr(self, '_chat_refresh_timer') and self._chat_refresh_timer:
                    self._chat_refresh_timer.stop()
                self._chat_refresh_timer = self.set_interval(
                    3.0, lambda: self._refresh_chat_feed())
                self._update_footer_status()
            try:
                self._call_on_main_thread(_activate_chat)
            except Exception:
                pass

        # Update UI to show agent connected (replaces "Waiting for agent...")
        # Skip _show_idle if chat view is now active — it would show #main-content
        if not self._chat_view_active:
            try:
                self._call_on_main_thread(self._show_idle)
            except Exception:
                pass

        # Speak the connection + fortune cookie
        try:
            import random as _random
            _fortunes = [
                "The code you write today is tomorrow's legacy.",
                "Every bug is a feature in disguise.",
                "Commit early, commit often, commit with conviction.",
                "The best error message is the one you never see.",
                "Today's refactor is tomorrow's relief.",
                "A well-named variable is worth a thousand comments.",
                "The compiler doesn't judge. Ship it.",
                "In the dance of curly braces, find your rhythm.",
                "Not all who wander through the codebase are lost.",
                "The tests that pass in silence protect the loudest.",
                "May your merges be clean and your deploys boring.",
                "A journey of a thousand lines begins with a single import.",
                "Trust the process. Also trust the tests.",
                "The scroll wheel spins, the code flows.",
                "Fortune favors the well-documented.",
            ]
            fortune = _random.choice(_fortunes)
            self._speak_ui(f"{session.name} connected. {fortune}")
        except Exception:
            pass

        # Emit for remote frontends
        try:
            frontend_api.emit_session_created(session.session_id, session.name)
        except Exception:
            _log.debug("Failed to emit session_created event", exc_info=True)

        # Notification webhook for session creation
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
            _log.debug("Failed to send agent_connected notification", exc_info=True)

    def on_session_removed(self, session_id: str) -> None:
        """Called when a session is removed."""
        # Capture name and resolve pending inbox items before removal
        removed_session = self.manager.get(session_id)
        removed_name = removed_session.name if removed_session else session_id

        # Resolve any pending inbox items so blocked MCP threads get a clean
        # cancellation instead of hanging forever.  manager.remove() also calls
        # _resolve_pending_inbox, but we do it here first to ensure the active
        # inbox item reference is cleaned up on the TUI side as well.
        if removed_session:
            _resolve_pending_inbox(removed_session)
            # Clear TUI-side active state so stale choices don't linger
            removed_session.active = False
            removed_session.preamble = ""
            removed_session.choices = []
            removed_session._active_inbox_item = None

        new_active = self.manager.remove(session_id)

        # Emit for remote frontends
        try:
            frontend_api.emit_session_removed(session_id)
        except Exception:
            _log.debug("Failed to emit session_removed event", exc_info=True)

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
            _log.debug("Failed to send agent_disconnected notification", exc_info=True)

        try:
            self._call_on_main_thread(self._update_tab_bar)
            # Force inbox list rebuild to remove stale items from dead session
            self._inbox_last_generation = -1
            self._call_on_main_thread(self._update_inbox_list)
            if new_active is not None:
                new_session = self.manager.get(new_active)
                if new_session:
                    self._call_on_main_thread(lambda s=new_session: self._switch_to_session(s))
            else:
                self._call_on_main_thread(self._show_idle)
        except Exception:
            _log.debug("on_session_removed: UI update failed", exc_info=True)

    # ─── Prompt replay ────────────────────────────────────────────

    def action_replay_prompt(self) -> None:
        """Replay just the preamble (works even after selection)."""
        session = self._focused()
        if not session or not session.preamble:
            return
        session.reading_options = False
        self._tts.stop()
        self._tts.speak_async(session.preamble)

    def action_replay_prompt_full(self) -> None:
        """Replay preamble + all options (works even after selection)."""
        session = self._focused()
        if not session or not session.preamble:
            return
        session.reading_options = False
        self._tts.stop()
        self._replay_prompt_worker(session)

    @work(thread=True, exit_on_error=False, name="replay_prompt", exclusive=True)
    def _replay_prompt_worker(self, session: Session) -> None:
        """Worker: replay preamble and all options in background thread."""
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

        # Haptic feedback on scroll (short buzz) — skip in settings menus
        if not self._in_settings and not self._setting_edit_mode:
            self._vibrate(30)

        # Chat bubble item: read clean TTS text (no markup/timestamps)
        if isinstance(event.item, ChatBubbleItem):
            text = getattr(event.item, 'tts_text', '')
            if text:
                now = time.time()
                last_time = getattr(self, '_last_spoken_time', 0.0)
                if text != self._last_spoken_text or (now - last_time) > 0.5:
                    self._last_spoken_text = text
                    self._last_spoken_time = now
                    ui_speed = self._config.tts_speed_for("ui") if self._config else None
                    self._tts.speak_with_local_fallback(text,
                                                        speed_override=ui_speed)
            return

        # Inbox list highlight: read preamble preview of highlighted item
        if isinstance(event.item, InboxListItem):
            # Skip TTS for done items — only read pending (unresolved) items.
            # Done items are visual history; reading them aloud after auto-focus
            # is confusing. The user can still select done items to review them.
            if event.item.is_done:
                self._inbox_scroll_index = event.item.inbox_index
                return

            preamble = event.item.inbox_preamble if event.item.inbox_preamble else "no preamble"
            n = event.item.n_choices
            status = f"{n} option{'s' if n != 1 else ''}"
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
                # Use _speak_ui for settings values — matches pregeneration voice
                # and self-interrupts on scroll (unlike speak_async which queues)
                self._speak_ui(val)
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
                        self._speak_ui(text)
                return
            # System logs: read the log entry text and show full entry in preamble
            if getattr(self, '_system_logs_mode', False):
                if isinstance(event.item, ChoiceItem):
                    entries = getattr(self, '_system_log_entries', [])
                    full_entries = getattr(self, '_system_log_full_entries', [])
                    idx = event.item.display_index
                    if idx < len(entries):
                        self._speak_ui(entries[idx])
                    # Show full entry in preamble for expanded view
                    if idx < len(full_entries):
                        full = full_entries[idx]
                        s = self._cs
                        # Try to pretty-format JSON log entries
                        formatted = full
                        try:
                            import json as _json
                            parsed = _json.loads(full)
                            parts = []
                            if "timestamp" in parsed:
                                parts.append(f"[{s['fg_dim']}]{parsed['timestamp']}[/{s['fg_dim']}]")
                            if "level" in parsed:
                                lvl = parsed["level"]
                                color = s['error'] if lvl in ("ERROR", "WARNING") else s['fg_dim']
                                parts.append(f"[{color}]{lvl}[/{color}]")
                            if "message" in parsed:
                                parts.append(f"[{s['fg']}]{parsed['message']}[/{s['fg']}]")
                            ctx = parsed.get("context", {})
                            if ctx:
                                ctx_parts = [f"{k}={v}" for k, v in ctx.items()]
                                parts.append(f"[{s['fg_dim']}]{', '.join(ctx_parts)}[/{s['fg_dim']}]")
                            if parts:
                                formatted = "\n".join(parts)
                        except (ValueError, TypeError, KeyError):
                            # Not JSON — wrap the raw text
                            formatted = f"[{s['fg']}]{full}[/{s['fg']}]"
                        try:
                            preamble = self.query_one("#preamble", Label)
                            preamble.update(formatted)
                        except Exception:
                            pass
                return
            # Help screen: read the shortcut description
            if getattr(self, '_help_mode', False):
                if isinstance(event.item, ChoiceItem):
                    idx = event.item.display_index
                    shortcuts = getattr(self, '_help_shortcuts', [])
                    if idx < len(shortcuts):
                        key, desc = shortcuts[idx]
                        self._speak_ui(f"{key}. {desc}")
                return
            # History mode: read the selection entry
            if getattr(self, '_history_mode', False):
                if isinstance(event.item, ChoiceItem) and session:
                    idx = event.item.display_index
                    history = getattr(session, 'history', [])
                    if idx < len(history):
                        entry = history[idx]
                        text = f"{entry.label}. {entry.summary}" if entry.summary else entry.label
                        self._speak_ui(text)
                return
            # Tab picker: switch to the highlighted tab live
            if getattr(self, '_tab_picker_mode', False):
                if isinstance(event.item, ChoiceItem):
                    idx = event.item.display_index
                    sessions = getattr(self, '_tab_picker_sessions', [])
                    if idx < len(sessions):
                        self._speak_ui(sessions[idx].name)
                return
            if isinstance(event.item, ChoiceItem):
                s = self._settings_items[event.item.display_index] if event.item.display_index < len(self._settings_items) else None
                if s:
                    text = f"{s['label']}. {s.get('summary', '')}" if s.get('summary') else s['label']
                    # Use _speak_ui — matches pregeneration voice and
                    # self-interrupts on scroll (unlike speak_async which queues)
                    self._speak_ui(text)
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
            is_extra = logical <= 0  # Extra options use UI voice
            if logical > 0:
                ci = logical - 1
                if ci >= len(session.choices):
                    return
                c = session.choices[ci]
                s = c.get('summary', '')
                # Build fragments for concatenated playback
                from io_mcp.tts import TTSEngine
                _num_words = TTSEngine._NUMBER_WORDS
                fragments = []
                if logical in _num_words:
                    fragments.append(_num_words[logical])
                label = c.get('label', '')
                if label:
                    fragments.append(label)
                if s:
                    fragments.append(s)
                # Boundary cue: "Top." for first, "Last." for last real choice
                boundary = ""
                if logical == 1:
                    boundary = "Top. "
                    fragments.insert(0, "Top")
                elif logical == len(session.choices):
                    boundary = "Last. "
                    fragments.insert(0, "Last")
                # Full text for dedup key and fallback
                text = f"{boundary}{logical}. {label}. {s}" if s else f"{boundary}{logical}. {label}"
            else:
                # Extra option or separator — use the widget's label directly
                # Strip Rich markup tags (e.g. [#616e88]...[/#616e88]) for TTS
                fragments = []
                raw_label = _strip_rich_markup(event.item.choice_label)
                # Skip separator items (like "─── Recent ───", "─── Activity ───")
                # that are purely decorative and contain only box-drawing chars
                if not raw_label or all(c in '─ \t' for c in raw_label):
                    return
                text = raw_label
                raw_summary = _strip_rich_markup(event.item.choice_summary) if event.item.choice_summary else ""
                if raw_summary:
                    fragments = [raw_label, raw_summary]
                    text = f"{text}. {raw_summary}"
            if text:
                # Deduplicate with cooldown — skip if same text was spoken very recently
                # but allow re-reading after a brief pause (e.g. scrolling away and back)
                now = time.time()
                last_time = getattr(self, '_last_spoken_time', 0.0)
                if text != self._last_spoken_text or (now - last_time) > 0.5:
                    self._last_spoken_text = text
                    self._last_spoken_time = now
                    # Use fragment-based playback for cached fragments,
                    # falling back to speak_with_local_fallback for uncached.
                    # Use UI speed for scroll readout (numbers, labels, summaries).
                    ui_speed = self._config.tts_speed_for("ui") if self._config else None
                    # Extra options use UI voice override to match how they
                    # were pregenerated (via _pregenerate_ui_worker). Without
                    # this, uiVoice configs cause cache key mismatches.
                    voice_ov = None
                    if is_extra and self._config:
                        ui_preset = self._config.tts_ui_voice_preset
                        if ui_preset and ui_preset != self._config.tts_voice_preset:
                            voice_ov = ui_preset
                    if fragments and len(fragments) > 1:
                        self._tts.speak_fragments_scroll(fragments,
                                                         voice_override=voice_ov,
                                                         speed_override=ui_speed)
                    else:
                        self._tts.speak_with_local_fallback(text,
                                                            voice_override=voice_ov,
                                                            speed_override=ui_speed)

            if self._dwell_time > 0:
                self._start_dwell()

    @on(ListView.Selected)
    def on_list_selected(self, event: ListView.Selected) -> None:
        """Handle Enter/click on a list item."""
        # Ignore selections from the chat feed — bubbles are read-only history
        try:
            chat_feed = self.query_one("#chat-feed", ListView)
            if event.list_view is chat_feed:
                return
        except Exception:
            pass

        # Check if this is a chat-choices selection (chat view scrollable choices)
        try:
            chat_choices = self.query_one("#chat-choices", ListView)
            if event.list_view is chat_choices and isinstance(event.item, ChoiceItem):
                # Multi-select mode: toggle/confirm instead of normal selection
                if self._multi_select_mode:
                    self._handle_multi_select_enter(event.item.display_index)
                    return
                # Handle just like normal choice selection
                session = self._focused()
                if not session or not session.active:
                    return
                logical = event.item.choice_index
                if logical > 0:
                    ci = logical - 1
                    if ci >= len(session.choices):
                        return
                    c = session.choices[ci]
                    label = c.get("label", "")
                    summary = c.get("summary", "")
                    self._tts.stop()
                    self._vibrate(100)
                    ui_speed = self._config.tts_speed_for("ui") if self._config else None
                    self._tts.speak_fragments(["selected", label], speed_override=ui_speed)
                    self._resolve_selection(session, {"selected": label, "summary": summary})
                    self.query_one("#chat-choices").display = False
                    self._chat_content_hash = ""
                    self._refresh_chat_feed()
                else:
                    # Extra option
                    self._handle_extra_select(event.item.choice_label)
                return
        except Exception:
            pass

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
        """Get the currently focused list view (inbox, choices, or chat-feed).

        Checks actual widget focus first to handle Tab key / click focus
        changes, then falls back to the logical _inbox_pane_focused state.
        """
        # Chat view: prefer #chat-choices if visible (scrollable selection),
        # otherwise fall through to chat-feed for scrolling the timeline
        if self._chat_view_active:
            try:
                chat_choices = self.query_one("#chat-choices", ListView)
                if chat_choices.display:
                    return chat_choices
            except Exception:
                pass
            try:
                return self.query_one("#chat-feed", ListView)
            except Exception:
                pass

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

    def _scroll_skip_count(self) -> int:
        """Compute how many items to skip based on scroll speed.

        Records the current timestamp in a ring buffer of the last 5 scroll
        events. If the average interval between the last 3+ events is below
        the turbo threshold, return turboSkip. If below the fast threshold,
        return fastSkip. Otherwise return 1 (normal scroll).
        """
        if not self._scroll_accel_enabled:
            return 1

        now = time.time()
        self._scroll_times.append(now)
        # Keep only the last 5 timestamps
        if len(self._scroll_times) > 5:
            self._scroll_times = self._scroll_times[-5:]

        # Need at least 3 timestamps to compute meaningful intervals
        if len(self._scroll_times) < 3:
            return 1

        # Compute average interval between consecutive timestamps (in ms)
        intervals = []
        for i in range(1, len(self._scroll_times)):
            intervals.append((self._scroll_times[i] - self._scroll_times[i - 1]) * 1000.0)

        avg_ms = sum(intervals) / len(intervals)

        if avg_ms <= self._scroll_accel_turbo_ms:
            return self._scroll_accel_turbo_skip
        elif avg_ms <= self._scroll_accel_fast_ms:
            return self._scroll_accel_fast_skip
        return 1

    def action_cursor_down(self) -> None:
        session = self._focused()
        if session and (session.input_mode or session.voice_recording):
            return
        self._interrupt_readout()
        list_view = self._active_list_view()
        if list_view.display:
            if not list_view.has_focus:
                list_view.focus()
            current = list_view.index
            children = list(list_view.children)
            if current is not None and len(children) > 0:
                # Build list of enabled indices
                enabled = [i for i, c in enumerate(children) if not c.disabled]
                if not enabled:
                    return

                skip = self._scroll_skip_count()

                # Find current position in enabled list
                try:
                    pos = enabled.index(current)
                except ValueError:
                    # Current index isn't enabled; find nearest enabled after current
                    pos = -1
                    for idx, e in enumerate(enabled):
                        if e >= current:
                            pos = idx
                            break
                    if pos == -1:
                        pos = len(enabled) - 1

                # Advance by skip items within enabled list
                new_pos = pos + skip
                if new_pos >= len(enabled):
                    # Wrap to first enabled item
                    list_view.index = enabled[0]
                else:
                    list_view.index = enabled[new_pos]
                return
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
            current = list_view.index
            children = list(list_view.children)
            if current is not None and len(children) > 0:
                # Build list of enabled indices
                enabled = [i for i, c in enumerate(children) if not c.disabled]
                if not enabled:
                    return

                skip = self._scroll_skip_count()

                # Find current position in enabled list
                try:
                    pos = enabled.index(current)
                except ValueError:
                    # Current index isn't enabled; find nearest enabled before current
                    pos = len(enabled)
                    for idx in range(len(enabled) - 1, -1, -1):
                        if enabled[idx] <= current:
                            pos = idx
                            break
                    if pos >= len(enabled):
                        pos = 0

                # Move back by skip items within enabled list
                new_pos = pos - skip
                if new_pos < 0:
                    # Wrap to last enabled item
                    list_view.index = enabled[-1]
                else:
                    list_view.index = enabled[new_pos]
                return
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
                ui_speed = self._config.tts_speed_for("ui") if self._config else None
                self._tts.speak_fragments(["selected", result], speed_override=ui_speed)
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

                # Refresh chat feed to show the queued message
                if self._chat_view_active:
                    self._chat_content_hash = ""  # Force rebuild
                    self._refresh_chat_feed()

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
        """Filter the choices ListView to show only matching items.

        Works with both ``#choices`` (normal inbox view) and
        ``#chat-choices`` (chat view).  In chat view the extras list is
        simpler (PRIMARY_EXTRAS only, no collapse toggle) and a
        PreambleItem header is prepended.
        """
        session = self._focused()
        if not session or not session.active:
            return

        in_chat = self._chat_view_active
        list_view = self.query_one(
            "#chat-choices" if in_chat else "#choices", ListView,
        )
        list_view.clear()
        q = query.lower()

        # In chat view, re-add preamble header (non-selectable)
        if in_chat and session.preamble:
            list_view.append(PreambleItem(session.preamble))

        # Build visible extras list — chat view uses PRIMARY_EXTRAS only
        if in_chat:
            visible_extras = list(PRIMARY_EXTRAS)
        elif self._extras_expanded:
            visible_extras = list(SECONDARY_EXTRAS) + list(PRIMARY_EXTRAS)
        else:
            visible_extras = [MORE_OPTIONS_ITEM] + list(PRIMARY_EXTRAS)

        # Filter real choices first (shown above extras in the list)
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

        # Extras (filtered too if query is set)
        for i, e in enumerate(visible_extras):
            logical_idx = -(len(visible_extras) - 1 - i)
            if q and q not in e["label"].lower() and q not in e.get("summary", "").lower():
                continue
            list_view.append(ChoiceItem(
                e["label"], e.get("summary", ""),
                index=logical_idx, display_index=i,
            ))

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
            lv_id = "#chat-choices" if self._chat_view_active else "#choices"
            list_view = self.query_one(lv_id, ListView)
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
        lv_id = "#chat-choices" if self._chat_view_active else "#choices"
        list_view = self.query_one(lv_id, ListView)
        list_view.focus()

    @on(SubmitTextArea.Submitted, "#chat-input")
    def on_chat_input_submitted(self, event: SubmitTextArea.Submitted) -> None:
        """Handle message submitted from the chat view input box."""
        if not self._chat_view_active:
            return
        try:
            chat_input = self.query_one("#chat-input", SubmitTextArea)
            message = chat_input.text.strip()
        except Exception:
            _log.warning("on_chat_input_submitted: failed to read chat input")
            return
        if not message:
            return
        session = self._focused()
        if not session:
            self._speak_ui("No active session")
            try:
                chat_input.clear()
            except Exception:
                pass
            return
        try:
            if session.active and session.choices:
                # Freeform selection — submit typed text as a choice response
                self._tts.stop()
                self._vibrate(100)
                ui_speed = self._config.tts_speed_for("ui") if self._config else None
                self._tts.speak_fragments(["selected", message], speed_override=ui_speed)
                self._resolve_selection(session, {"selected": message, "summary": "(freeform input)"})
                try:
                    self.query_one("#chat-choices").display = False
                except Exception:
                    pass
            else:
                # No active choices — queue as a pending message for the agent
                session.pending_messages.append(message)
                self._speak_ui("Message queued")
        except Exception:
            _log.exception("on_chat_input_submitted: error processing message")
        # Always clear input and refresh feed
        try:
            chat_input.clear()
        except Exception:
            pass
        self._chat_content_hash = ""
        self._refresh_chat_feed()

    def on_click(self, event) -> None:
        """Handle clicks — check if the voice button was clicked."""
        try:
            if not self._chat_view_active:
                return
            widget = getattr(event, "widget", None)
            if widget is not None and getattr(widget, "id", None) == "chat-voice-btn":
                self.action_voice_input()
        except Exception:
            _log.debug("on_click: error handling click", exc_info=True)

    @on(VoiceButton.Pressed, "#chat-voice-btn")
    def on_voice_button_pressed(self, event: VoiceButton.Pressed) -> None:
        """Handle Enter on the voice button — start voice recording."""
        try:
            self.action_voice_input()
        except Exception:
            _log.debug("on_voice_button_pressed: error", exc_info=True)

    def _on_voice_button_focus(self) -> None:
        """Speak 'Voice input' when the chat voice button receives focus."""
        try:
            self._speak_ui("Voice input")
        except Exception:
            _log.debug("_on_voice_button_focus: error", exc_info=True)

    def on_descendant_focus(self, event) -> None:
        """Detect when the voice button receives focus and speak TTS."""
        widget = event.widget if hasattr(event, 'widget') else None
        if widget is not None and getattr(widget, "id", None) == "chat-voice-btn":
            self._on_voice_button_focus()

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
        dashboard, settings, dialogs, spawn menu, tab picker, chat view,
        and setting edit mode. Blocked only during text input and voice recording.
        """
        session = self._focused()
        if not session:
            return
        if session.input_mode or session.voice_recording:
            return

        # Chat view: select choice directly by number
        if self._chat_view_active and session.active and session.choices:
            if n < 1 or n > len(session.choices):
                return
            c = session.choices[n - 1]
            label = c.get("label", "")
            summary = c.get("summary", "")
            self._tts.stop()
            self._vibrate(100)
            ui_speed = self._config.tts_speed_for("ui") if self._config else None
            self._tts.speak_fragments(["selected", label], speed_override=ui_speed)
            self._resolve_selection(session, {"selected": label, "summary": summary})
            self._chat_content_hash = ""
            self._refresh_chat_feed()
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

            # Setting edit mode — number picks from value list
            # Must check BEFORE _settings_items since that's still set
            # during edit mode (it's the parent menu items).
            if self._setting_edit_mode:
                list_view = self.query_one("#choices", ListView)
                if 1 <= n <= len(list_view.children):
                    list_view.index = n - 1
                    self._apply_setting_edit()
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

        # Multi-select mode: toggle checkbox or trigger action buttons
        if self._multi_select_mode:
            num_choices = len(self._multi_select_checked)
            # n is 1-based choice number
            choice_idx = n - 1
            if 0 <= choice_idx < num_choices:
                # Toggle choice checkbox
                self._multi_select_checked[choice_idx] = not self._multi_select_checked[choice_idx]
                label = session.choices[choice_idx].get("label", "")
                state = "selected" if self._multi_select_checked[choice_idx] else "unselected"
                self._tts.speak_async(f"{label} {state}")
                self._refresh_multi_select()
            elif choice_idx == num_choices:
                # Confirm (first button after choices)
                self._confirm_multi_select(team=False)
            elif choice_idx == num_choices + 1:
                # Team mode
                self._confirm_multi_select(team=True)
            elif choice_idx == num_choices + 2:
                # Cancel
                self._multi_select_mode = False
                self._multi_select_checked = []
                self._speak_ui("Multi-select cancelled.")
                self._show_choices()
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

            # Re-pregenerate common UI texts after cache clear
            self._pregenerate_common_ui_texts()

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

        self._tts.stop()

        # Audio cue — instant chime before TTS speech starts generating
        self._tts.play_chime("select")

        # Use fragment playback: "selected" + label are cached individually
        ui_speed = self._config.tts_speed_for("ui") if self._config else None
        self._tts.speak_fragments(["selected", label], speed_override=ui_speed)

        # Record in history
        try:
            session.append_history(HistoryEntry(
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

        # Auto-advance: if another session has pending choices, switch to it
        # immediately so the user doesn't have to hit "n"
        self._auto_advance_to_next_choices(session)

    def _auto_advance_to_next_choices(self, current_session: Session) -> None:
        """Auto-switch to the next session with pending choices.

        Called after a selection is made. If the current session has no
        more pending choices, finds another session that does and switches
        to it immediately, so the user doesn't have to press "n".
        """
        # Check if current session still has pending choices (from same session)
        if current_session.inbox_choices_count() > 0:
            return  # Same session has more — drain loop will present them

        # Find another session with pending choices
        next_session = self.manager.next_with_choices()
        if next_session and next_session.session_id != current_session.session_id:
            # Brief delay so "Selected: X" audio has a moment to start
            import time as _time
            _time.sleep(0.3)
            self._switch_to_session(next_session)

    def _handle_extra_select(self, label: str) -> None:
        """Handle selection of extra options by label."""
        self._tts.stop()
        self._vibrate(100)  # Haptic feedback on extra selection

        if label == "More options ›" or label == "More options":
            # Toggle expand/collapse and re-render
            if self._chat_view_active:
                # Chat view has its own expand state
                self._chat_extras_expanded = not getattr(self, '_chat_extras_expanded', False)
                session = self._focused()
                if session and session.active:
                    self._populate_chat_choices_list(session)
            else:
                self._extras_expanded = not self._extras_expanded
                self._show_choices()
            expanded = (self._chat_extras_expanded if self._chat_view_active
                        else self._extras_expanded)
            if expanded:
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
        elif label == "Dismiss":
            self._dismiss_active_item()
        elif label == "Quick settings":
            self._enter_quick_settings()
        elif label == "History":
            self._show_history()
        elif label == "Queue message":
            self.action_queue_message()
        elif label == "Help":
            self.action_show_help()
        elif label == "Type reply":
            self.action_freeform_input()
        elif label == "Undo":
            self.action_undo_selection()
        elif label == "Replay prompt":
            self.action_replay_prompt()
        elif label == "Chat view":
            self.action_chat_view()
        elif label == "Filter":
            self.action_filter_choices()

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

        # Pregenerate quick settings labels + summaries for instant scroll TTS
        qs_texts = set()
        for item in items:
            label = item.get("label", "")
            summary = item.get("summary", "")
            if label:
                qs_texts.add(label)
            if summary:
                qs_texts.add(f"{label}. {summary}")
        if qs_texts:
            self._pregenerate_ui_worker(list(qs_texts))

    def _handle_quick_settings_select(self, label: str) -> None:
        """Handle selection in the quick settings submenu."""
        self._tts.stop()
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

        In chat view: hides the #chat-choices panel, removes the undone
        item from inbox so it doesn't appear as a resolved choice with
        an "_undo" result label in the chat feed, and force-refreshes
        the feed. The server loop then re-presents the same choices,
        which triggers _show_choices → _populate_chat_choices_list.
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
        self._tts.play_chime("undo")
        self._speak_ui("Undoing selection")

        # Remove the active inbox item from both inbox and inbox_done
        # so it doesn't appear as a resolved choice in the chat feed.
        # If it's still in inbox, removing it prevents peek_inbox (backend
        # thread) from moving it to inbox_done with an "_undo" result.
        # If it already reached inbox_done (backend already processed it),
        # remove it there so the chat feed drops the "selected" indicator.
        # The item reference is still held by _activate_and_present so
        # item.event.wait() + item.result still work correctly.
        item = getattr(session, '_active_inbox_item', None)
        if item:
            if item in session.inbox:
                session.inbox.remove(item)
                session._inbox_generation += 1
            elif item in session.inbox_done:
                session.inbox_done.remove(item)
                session._inbox_generation += 1

        # Re-activate the session with the saved choices
        self._resolve_selection(session, {"selected": "_undo", "summary": ""})

        # Chat view: hide choices panel and refresh feed so the undone
        # item's "selected" indicator is removed from the timeline.
        if self._chat_view_active:
            try:
                self.query_one("#chat-choices").display = False
            except Exception:
                pass
            self._chat_content_hash = ""  # Force rebuild
            self._refresh_chat_feed()

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
        options = [
            {"label": "Local agent", "summary": "Spawn Claude Code on this machine in a new tmux window",
             "_agent": "io-mcp:io-mcp"},
            {"label": "Local admin agent", "summary": "Spawn admin agent for backlog, swarm, and PR management",
             "_agent": "io-mcp:io-mcp-admin"},
        ]

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
                    "_agent": "io-mcp:io-mcp",
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
        agent = option.get("_agent", "io-mcp:io-mcp")

        if label == "Cancel":
            self._exit_settings()
            return

        self._speak_ui(f"Spawning {label}")
        self._do_spawn_worker(label, host, workdir, agent)

    @work(thread=True, exit_on_error=False, name="spawn_agent")
    def _do_spawn_worker(self, label: str, host: str, workdir: str, agent: str) -> None:
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
                    f'claude --agent {agent} "connect to io-mcp and greet the user"'
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
                    f'IO_MCP_URL={io_mcp_url} claude --agent {agent} "connect to io-mcp and greet the user"',
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
                self._show_choices()

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
