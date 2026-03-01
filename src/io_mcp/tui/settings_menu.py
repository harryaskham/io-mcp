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


def _format_byte_size(n: int) -> str:
    """Format a byte count as a human-readable size string (B, KB, or MB)."""
    if n >= 1_048_576:
        return f"{n / 1_048_576:.1f} MB"
    elif n >= 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n} B"


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

        # Remember if chat view was active so we can restore on exit
        self._settings_was_chat_view = getattr(self, '_chat_view_active', False)

        # If chat view is active, temporarily hide it and show main-content
        if self._settings_was_chat_view:
            try:
                self.query_one("#chat-feed").display = False
                self.query_one("#chat-choices").display = False
                self.query_one("#chat-input-bar").display = False
            except Exception:
                pass
            # Stop chat refresh timer
            if hasattr(self, '_chat_refresh_timer') and self._chat_refresh_timer:
                self._chat_refresh_timer.stop()
                self._chat_refresh_timer = None

        scheme = getattr(self, '_color_scheme', DEFAULT_SCHEME)

        # Build current voice display: preset name
        current_voice = self.settings.voice

        # UI voice display
        ui_voice = ""
        if self._config:
            ui_voice = self._config.tts_ui_voice_preset
        ui_voice_display = ui_voice if ui_voice and ui_voice != self.settings.voice else "same as agent"

        # Build local TTS display
        local_backend = "termux"
        if self._config:
            local_backend = self._config.tts_local_backend

        # TTS cache stats
        cache_count, cache_bytes = self._tts.cache_stats()
        cache_size_str = _format_byte_size(cache_bytes)
        cache_summary = f"{cache_count} items ({cache_size_str})" if cache_count else "empty"

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
            {"label": "TTS cache", "key": "tts_cache",
             "summary": cache_summary},
            {"label": "Close settings", "key": "close", "summary": ""},
        ]

        preamble_widget = self.query_one("#preamble", Label)
        preamble_widget.update("Settings")
        preamble_widget.display = True

        # Ensure main content container is visible, hide inbox pane in settings
        # In chat view, _ensure_main_content_visible is a no-op, so directly show it
        if self._settings_was_chat_view:
            try:
                mc = self.query_one("#main-content")
                mc.display = True
                mc.styles.height = "1fr"
                self.query_one("#inbox-list").display = False
            except Exception:
                pass
        else:
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

        # Pregenerate settings labels + summaries for instant scroll TTS.
        # Labels like "Speed", "Agent voice" etc. are read via speak_async
        # on highlight, which checks the cache. Pregenerate with UI voice
        # so cache keys match the speak_async path.
        settings_texts = set()
        for s in self._settings_items:
            label = s.get("label", "")
            summary = s.get("summary", "")
            if label:
                settings_texts.add(label)
            # Combine label.summary for the full TTS string read on highlight
            if summary:
                settings_texts.add(f"{label}. {summary}")
        if settings_texts:
            self._pregenerate_ui_worker(list(settings_texts))

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
        try:
            pane_view = self.query_one("#pane-view", RichLog)
            if pane_view.display:
                pane_view.display = False
                if hasattr(self, '_pane_refresh_timer') and self._pane_refresh_timer:
                    self._pane_refresh_timer.stop()
                    self._pane_refresh_timer = None
                self._pane_view_was_chat = False
        except Exception:
            pass

        # Clean up chat view if active
        if getattr(self, '_chat_view_active', False):
            self._chat_view_active = False
            if hasattr(self, '_chat_refresh_timer') and self._chat_refresh_timer:
                self._chat_refresh_timer.stop()
                self._chat_refresh_timer = None
            try:
                self.query_one("#chat-feed").display = False
            except Exception:
                pass
            try:
                self.query_one("#chat-choices").display = False
            except Exception:
                pass
            try:
                self.query_one("#chat-input-bar").display = False
            except Exception:
                pass

    def _exit_settings(self) -> None:
        """Leave settings and restore choices."""
        session = self._focused()
        was_chat_view = getattr(self, '_settings_was_chat_view', False)
        self._clear_all_modal_state(session=session)

        # Guard: prevent the Enter keypress that triggered "close" from
        # also firing _do_select on the freshly-restored choice list.
        self._settings_just_closed = True
        self.set_timer(0.3, self._clear_settings_guard)

        # Restore chat view if it was active before settings
        if was_chat_view:
            self._chat_view_active = True
            try:
                self.query_one("#chat-feed").display = True
                self.query_one("#chat-input-bar").display = True
                self.query_one("#main-content").display = False
                self.query_one("#inbox-list").display = False
                self.query_one("#preamble").display = False
                self.query_one("#status").display = False
            except Exception:
                pass
            # Determine unified mode
            all_sessions = list(self.manager.all_sessions()) if hasattr(self, 'manager') else []
            self._chat_unified = len(all_sessions) > 1
            # Rebuild feed
            if session:
                self._chat_content_hash = ""
                self._chat_force_full_rebuild = True  # Theme may have changed
                if self._chat_unified:
                    self._build_chat_feed(session, sessions=all_sessions)
                else:
                    self._build_chat_feed(session)
            # Restart refresh timer
            if hasattr(self, '_chat_refresh_timer') and self._chat_refresh_timer:
                self._chat_refresh_timer.stop()
            self._chat_refresh_timer = self.set_interval(
                3.0, lambda: self._refresh_chat_feed())
            self._update_footer_status()
            self._tts.stop()
            self._speak_ui("Back to chat")
            return

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
        if key == "tts_cache":
            # Show clear cache option
            self._setting_edit_mode = True
            self._setting_edit_key = key
            self._setting_edit_values = ["Clear cache", "Back"]
            self._setting_edit_index = 0

            list_view = self.query_one("#choices", ListView)
            list_view.clear()
            cache_count, cache_bytes = self._tts.cache_stats()
            size_str = _format_byte_size(cache_bytes)
            list_view.append(ChoiceItem("Clear cache", f"Remove {cache_count} items ({size_str})", index=1, display_index=0))
            list_view.append(ChoiceItem("Back", "", index=2, display_index=1))
            list_view.index = 0
            list_view.focus()

            self._tts.stop()
            self._speak_ui(f"TTS cache: {cache_count} items, {size_str}. Select Clear to remove.")
            return

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
            # Voice preset names
            self._setting_edit_values = self.settings.get_voices()
            current_voice = self.settings.voice
            self._setting_edit_index = (
                self._setting_edit_values.index(current_voice)
                if current_voice in self._setting_edit_values else 0
            )

        elif key == "ui_voice":
            # Voice preset names, plus "same as agent" option
            presets = self.settings.get_voices()
            self._setting_edit_values = ["same as agent"] + presets
            ui_voice = ""
            if self._config:
                ui_voice = self._config.tts_ui_voice_preset
            self._setting_edit_index = 0
            if ui_voice and ui_voice != self.settings.voice and ui_voice in presets:
                self._setting_edit_index = presets.index(ui_voice) + 1  # +1 for "same as agent"

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

        # Pregenerate setting edit values in background for instant scroll TTS.
        # All values are pregenerated (not just speed/voice) since scrolling
        # through styles, STT models, color schemes etc. should also be instant.
        self._pregenerate_ui_worker(list(self._setting_edit_values))

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
            # Direct preset name
            self.settings.voice = value
        elif key == "ui_voice":
            if self._config:
                if value == "same as agent":
                    self._config.raw.setdefault("config", {}).setdefault("tts", {})["uiVoice"] = ""
                else:
                    self._config.raw.setdefault("config", {}).setdefault("tts", {})["uiVoice"] = value
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
        elif key == "tts_cache":
            if value == "Clear cache":
                self._tts.clear_cache()
                self._setting_edit_mode = False
                self._tts.stop()
                self._speak_ui("Cache cleared")
                self._enter_settings()
                return
            else:
                # "Back" — return to settings without clearing
                self._setting_edit_mode = False
                self._enter_settings()
                return

        self._tts.clear_cache()

        self._setting_edit_mode = False
        self._tts.stop()
        self._speak_ui(f"{key} set to {value}")

        self._enter_settings()

