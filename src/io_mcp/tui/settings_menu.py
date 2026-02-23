"""Settings menu mixin for IoMcpApp.

Contains the settings menu display and navigation logic.
Mixed into IoMcpApp via multiple inheritance.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

from textual.widgets import Label, ListView, RichLog

from .themes import get_scheme, DEFAULT_SCHEME
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
        self._history_mode = False
        self._tab_picker_mode = False

        # Clean up pane viewer if active
        if getattr(self, '_pane_viewer_mode', False):
            self._pane_viewer_mode = False
            self._stop_pane_refresh()
            try:
                self.query_one("#pane-view", RichLog).display = False
            except Exception:
                pass

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
            marker = " *" if i == self._setting_edit_index else ""
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

