"""Settings menu mixin for IoMcpApp.

Contains the settings menu display and navigation logic.
Mixed into IoMcpApp via multiple inheritance.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from textual.widgets import Label, ListView, RichLog

from .themes import get_scheme, build_css, DEFAULT_SCHEME, COLOR_SCHEMES
from .widgets import ChoiceItem, _safe_action

if TYPE_CHECKING:
    from .app import IoMcpApp


class SettingsMixin:
    """Mixin providing settings menu action methods."""

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

        # Build current voice display: "voice (model)"
        current_voice = f"{self.settings.voice} ({self.settings.tts_model})"

        # UI voice display
        ui_voice = ""
        if self._config:
            ui_voice = self._config.tts_ui_voice
        ui_voice_display = f"{ui_voice} ({self.settings.tts_model})" if ui_voice and ui_voice != self.settings.voice else "same as agent"

        # Build local TTS display
        local_backend = "termux"
        if self._config:
            local_backend = self._config.tts_local_backend

        self._settings_items = [
            {"label": "Speed", "key": "speed",
             "summary": f"Current: {self.settings.speed:.1f}"},
            {"label": "Agent voice", "key": "voice",
             "summary": f"Current: {current_voice}"},
            {"label": "UI voice", "key": "ui_voice",
             "summary": f"Current: {ui_voice_display}"},
            {"label": "Style", "key": "style",
             "summary": f"Current: {self.settings.emotion}"},
            {"label": "STT model", "key": "stt_model",
             "summary": f"Current: {self.settings.stt_model}"},
            {"label": "Local TTS", "key": "local_tts",
             "summary": f"Current: {local_backend}"},
            {"label": "Color scheme", "key": "color_scheme",
             "summary": f"Current: {scheme}"},
            {"label": "Close settings", "key": "close", "summary": ""},
        ]

        preamble_widget = self.query_one("#preamble", Label)
        preamble_widget.update("Settings")
        preamble_widget.display = True

        # Ensure main content container is visible, hide inbox pane in settings
        self._ensure_main_content_visible(show_inbox=False)

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
        self._speak_ui("Settings")

    def _clear_all_modal_state(self, *, session=None) -> None:
        """Reset ALL modal/menu state flags to their defaults.

        This is the single source of truth for clearing nested menu state.
        Called from both ``_exit_settings`` (user-initiated) and
        ``_activate_and_present`` (force-exit when choices arrive).

        Args:
            session: If provided, also clears per-session settings flags.
        """
        if session:
            session.in_settings = False
            session.reading_options = False

        self._in_settings = False
        self._setting_edit_mode = False
        self._spawn_options = None
        self._quick_action_options = None
        self._system_logs_mode = False
        self._help_mode = False
        self._history_mode = False
        self._tab_picker_mode = False
        self._quick_settings_mode = False
        self._worktree_options = None

        # Clear dialog state — dialogs set _in_settings too
        self._dialog_callback = None
        self._dialog_buttons = []

        # Clean up pane viewer if active
        if getattr(self, '_pane_viewer_mode', False):
            self._pane_viewer_mode = False
            self._stop_pane_refresh()
            try:
                self.query_one("#pane-view", RichLog).display = False
            except Exception:
                pass

    def _exit_settings(self) -> None:
        """Leave settings and restore choices."""
        session = self._focused()
        self._clear_all_modal_state(session=session)

        # Guard: prevent the Enter keypress that triggered "close" from
        # also firing _do_select on the freshly-restored choice list.
        self._settings_just_closed = True
        self.set_timer(0.3, self._clear_settings_guard)

        # UI first, then TTS
        if session and session.active:
            self._show_choices()
            self._tts.stop()
            self._speak_ui("Back to choices")
        else:
            self._show_idle()
            self._tts.stop()
            self._speak_ui("Settings closed")

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
            # Combined voice+model pairs: "sage (gpt-4o-mini-tts)"
            pairs = self.settings.get_voice_model_pairs()
            self._voice_model_pairs = pairs
            self._setting_edit_values = [
                f"{voice} ({model})" for voice, model in pairs
            ]
            current_voice = self.settings.voice
            current_model = self.settings.tts_model
            self._setting_edit_index = 0
            for i, (v, m) in enumerate(pairs):
                if v == current_voice and m == current_model:
                    self._setting_edit_index = i
                    break

        elif key == "ui_voice":
            # Same pairs as voice, plus "same as agent" option
            pairs = self.settings.get_voice_model_pairs()
            self._voice_model_pairs = [("", "")] + pairs  # empty = same as agent
            self._setting_edit_values = ["same as agent"] + [
                f"{voice} ({model})" for voice, model in pairs
            ]
            ui_voice = ""
            if self._config:
                ui_voice = self._config.tts_ui_voice
            self._setting_edit_index = 0
            if ui_voice and ui_voice != self.settings.voice:
                for i, (v, m) in enumerate(self._voice_model_pairs):
                    if v == ui_voice:
                        self._setting_edit_index = i
                        break

        elif key == "style":
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

        elif key == "local_tts":
            # Available local TTS backends
            from ..tts import _find_binary
            values = []
            if _find_binary("termux-exec"):
                values.append("termux")
            if _find_binary("espeak-ng"):
                values.append("espeak")
            values.append("none")
            self._setting_edit_values = values
            current = self._config.tts_local_backend if self._config else "termux"
            self._setting_edit_index = (
                values.index(current)
                if current in values else 0
            )

        # UI first
        list_view = self.query_one("#choices", ListView)
        list_view.clear()
        for i, val in enumerate(self._setting_edit_values):
            marker = " *" if i == self._setting_edit_index else ""
            list_view.append(ChoiceItem(f"{val}{marker}", "", index=i+1, display_index=i))
        list_view.index = self._setting_edit_index
        list_view.focus()

        # TTS after UI
        self._tts.stop()
        current_val = self._setting_edit_values[self._setting_edit_index]
        self._speak_ui(f"Editing {key}. Current: {current_val}. Scroll to change, Enter to confirm.")

        # Pregenerate in background
        if key in ("speed", "voice"):
            self._pregenerate_worker(list(self._setting_edit_values))

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
            # Combined voice+model pair
            pairs = getattr(self, '_voice_model_pairs', [])
            if idx < len(pairs):
                voice, model = pairs[idx]
                self.settings.set_voice_and_model(voice, model)
                value = f"{voice} ({model})"
        elif key == "ui_voice":
            pairs = getattr(self, '_voice_model_pairs', [])
            if idx < len(pairs):
                voice, model = pairs[idx]
                if self._config:
                    if voice:
                        self._config.raw.setdefault("config", {}).setdefault("tts", {})["uiVoice"] = voice
                    else:
                        # "same as agent" — clear uiVoice
                        self._config.raw.setdefault("config", {}).setdefault("tts", {})["uiVoice"] = ""
                    self._config.save()
        elif key == "style":
            self.settings.emotion = value
        elif key == "stt_model":
            self.settings.stt_model = value
        elif key == "color_scheme":
            self._color_scheme = value
            self._cs = get_scheme(value)
            self.__class__.CSS = build_css(value)
            # Save to config
            if self._config:
                self._config.raw.setdefault("config", {})["colorScheme"] = value
                self._config.save()
            self.title = "io-mcp"
        elif key == "local_tts":
            if self._config:
                self._config.raw.setdefault("config", {}).setdefault("tts", {})["localBackend"] = value
                self._config.save()
                # Update the TTS engine's local backend preference
                self._tts._local_backend = value

        self._tts.clear_cache()

        self._setting_edit_mode = False
        self._tts.stop()
        self._speak_ui(f"{key} set to {value}")

        self._enter_settings()

