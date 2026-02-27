"""View action mixins for IoMcpApp.

Contains pane view, help screen, and system log viewer action methods.
These are mixed into IoMcpApp via multiple inheritance.
"""

from __future__ import annotations

import os
import subprocess
import time
from typing import TYPE_CHECKING

from textual import work
from textual.widgets import Label, ListView, RichLog

from ..logging import read_log_tail, TUI_ERROR_LOG, PROXY_LOG
from .themes import DEFAULT_SCHEME, get_scheme
from .widgets import ChoiceItem, _safe_action

if TYPE_CHECKING:
    from .app import IoMcpApp


class ViewsMixin:
    """Mixin providing pane view, help screen, and system log viewer actions."""

    def _close_session(self: "IoMcpApp", session) -> None:
        """Close a session tab without killing the tmux pane."""
        name = session.name
        self.on_session_removed(session.session_id)
        self._speak_ui(f"Closed {name}")
        self._exit_settings()

    def _kill_session(self: "IoMcpApp", session) -> None:
        """Kill a session's tmux pane and close the tab."""
        import subprocess as sp

        name = session.name
        pane = getattr(session, 'tmux_pane', '')
        hostname = getattr(session, 'hostname', '')

        if pane:
            try:
                is_remote = hostname and hostname not in ("", "localhost", os.uname().nodename)
                if is_remote:
                    cmd = ["ssh", "-o", "ConnectTimeout=2", hostname,
                           f"tmux kill-pane -t {pane}"]
                else:
                    cmd = ["tmux", "kill-pane", "-t", pane]
                sp.run(cmd, capture_output=True, timeout=5)
            except Exception:
                pass

        self.on_session_removed(session.session_id)
        self._speak_ui(f"Killed {name}")
        self._exit_settings()

    @_safe_action
    def action_pane_view(self: "IoMcpApp") -> None:
        """Show live tmux pane output for the focused agent.

        Uses tmux capture-pane locally, or ssh+tmux for remote agents.
        Auto-refreshes every 2 seconds. Press v or Escape to close.
        """
        # Toggle off if already in pane view
        pane_view = self.query_one("#pane-view", RichLog)
        if pane_view.display:
            pane_view.display = False
            if hasattr(self, '_pane_refresh_timer') and self._pane_refresh_timer:
                self._pane_refresh_timer.stop()
                self._pane_refresh_timer = None
            self._speak_ui("Pane view closed.")
            session = self._focused()
            if session and session.active:
                self._show_choices()
            return

        session = self._focused()
        if not session:
            self._speak_ui("No active session")
            return
        if session.input_mode or session.voice_recording:
            return
        if self._in_settings or self._filter_mode:
            return

        pane = getattr(session, 'tmux_pane', '')
        hostname = getattr(session, 'hostname', '')

        if not pane:
            self._speak_ui("No tmux pane registered for this agent.")
            return

        self._tts.stop()
        self._speak_ui(f"Pane view for {session.name}. Press v to close.")

        # Show pane view, hide main content
        self.query_one("#main-content").display = False
        self.query_one("#preamble").display = False
        pane_view.clear()
        pane_view.display = True

        # Store pane info for the worker
        self._pane_view_hostname = hostname
        self._pane_view_pane = pane

        # Initial refresh
        self._refresh_pane_worker()

        # Auto-refresh every 2 seconds
        self._pane_refresh_timer = self.set_interval(
            2.0, lambda: self._refresh_pane_worker())

    @work(thread=True, exit_on_error=False, name="refresh_pane", exclusive=True)
    def _refresh_pane_worker(self: "IoMcpApp") -> None:
        """Worker: capture tmux pane content in background thread."""
        hostname = getattr(self, '_pane_view_hostname', '')
        pane = getattr(self, '_pane_view_pane', '')
        try:
            is_remote = hostname and hostname not in ("", "localhost", os.uname().nodename)
            if is_remote:
                cmd = ["ssh", "-o", "ConnectTimeout=2", hostname,
                       f"tmux capture-pane -p -t {pane} -S -50"]
            else:
                cmd = ["tmux", "capture-pane", "-p", "-t", pane, "-S", "-50"]

            result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
            if result.returncode == 0:
                content = result.stdout
                try:
                    self.call_from_thread(lambda: self._update_pane_view(content))
                except Exception:
                    pass
        except Exception:
            pass

    def _update_pane_view(self: "IoMcpApp", content: str) -> None:
        """Update the pane view widget with captured tmux output."""
        try:
            pane_view = self.query_one("#pane-view", RichLog)
            if pane_view.display:
                pane_view.clear()
                for line in content.split("\n"):
                    pane_view.write(line)
        except Exception:
            pass

    def _send_to_agent_pane(self: "IoMcpApp", session, message: str) -> None:
        """Send a message directly to an agent's tmux pane via tmux-cli.

        This bypasses the MCP message queue and injects text directly into
        the agent's Claude Code input, allowing interruption of long operations.

        Uses tmux-cli for reliable delivery (handles Enter key verification).
        Falls back to tmux send-keys if tmux-cli is unavailable.

        For remote agents, prepends ssh to the command.
        """
        pane = getattr(session, 'tmux_pane', '')
        if not pane:
            self._speak_ui("No tmux pane registered for this agent.")
            return

        hostname = getattr(session, 'hostname', '')
        is_remote = hostname and hostname not in (
            "", "localhost", os.uname().nodename,
        )
        # Also check against Tailscale self-hostname
        if is_remote:
            try:
                result = subprocess.run(
                    ["tailscale", "status", "--json"],
                    capture_output=True, text=True, timeout=3,
                )
                if result.returncode == 0:
                    import json
                    ts_data = json.loads(result.stdout)
                    self_dns = ts_data.get("Self", {}).get("DNSName", "").rstrip(".")
                    if self_dns and hostname.rstrip(".") == self_dns:
                        is_remote = False
            except Exception:
                pass

        session_name = session.name
        self._speak_ui(f"Sending to {session_name}")
        self._send_to_agent_pane_worker(pane, hostname, is_remote, message, session_name)

    @work(thread=True, exit_on_error=False, name="send_to_pane")
    def _send_to_agent_pane_worker(
        self: "IoMcpApp", pane: str, hostname: str, is_remote: bool,
        message: str, session_name: str,
    ) -> None:
        """Worker: send message to agent's tmux pane in background thread."""
        try:
            # Try tmux-cli first (more reliable with Enter key handling)
            tmux_cli = None
            try:
                result = subprocess.run(
                    ["which", "tmux-cli"], capture_output=True, text=True, timeout=3,
                )
                if result.returncode == 0:
                    tmux_cli = result.stdout.strip()
            except Exception:
                pass

            if tmux_cli:
                if is_remote:
                    cmd = [
                        "ssh", "-o", "ConnectTimeout=5", hostname,
                        f"tmux-cli send {repr(message)} --pane={pane}",
                    ]
                else:
                    cmd = [tmux_cli, "send", message, f"--pane={pane}"]
            else:
                # Fallback to tmux send-keys
                if is_remote:
                    cmd = [
                        "ssh", "-o", "ConnectTimeout=5", hostname,
                        f"tmux send-keys -t {pane} {repr(message)} Enter",
                    ]
                else:
                    cmd = ["tmux", "send-keys", "-t", pane, message, "Enter"]

            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0:
                try:
                    self.call_from_thread(
                        lambda: self._speak_ui(f"Message sent to {session_name}")
                    )
                except Exception:
                    pass
            else:
                err = result.stderr.strip()[:100] if result.stderr else "unknown error"
                try:
                    self.call_from_thread(
                        lambda: self._speak_ui(f"Failed to send: {err}")
                    )
                except Exception:
                    pass
        except Exception as e:
            try:
                self.call_from_thread(
                    lambda: self._speak_ui(f"Send error: {str(e)[:80]}")
                )
            except Exception:
                pass

    def _action_interrupt_agent(self: "IoMcpApp") -> None:
        """Open text input modal to send a message directly to the agent's tmux pane.

        Unlike queue message (m), this interrupts the agent immediately by
        injecting text into its Claude Code input via tmux-cli.
        """
        session = self._message_target()
        if not session:
            return
        if not getattr(session, 'tmux_pane', ''):
            self._speak_ui("No tmux pane for this agent.")
            return

        # Set interrupt mode — the modal dismiss callback uses this
        self._message_mode = True
        self._interrupt_mode = True
        self._message_target_session = session
        self._freeform_spoken_pos = 0
        self._inbox_was_visible = self._inbox_pane_visible()

        self._tts.stop()
        self._speak_ui(f"Type message to interrupt {session.name}")

        # Use the shared queue_message modal flow — it checks _interrupt_mode
        from .widgets import TextInputModal

        def _on_interrupt_dismiss(result):
            from .widgets import VOICE_REQUESTED
            inbox_was_visible = self._inbox_was_visible

            if result == VOICE_REQUESTED:
                self.action_voice_input()
                return

            self._message_mode = False
            self._interrupt_mode = False
            self._inbox_was_visible = False
            target = self._message_target_session or session
            self._message_target_session = None

            if result is None:
                if target.active:
                    self._show_choices()
                else:
                    self._ensure_main_content_visible(show_inbox=inbox_was_visible)
                    self._show_session_waiting(target)
                self._speak_ui("Cancelled.")
            else:
                self._vibrate(100)
                self._send_to_agent_pane(target, result)
                if target.active:
                    self._show_choices()
                else:
                    self._ensure_main_content_visible(show_inbox=inbox_was_visible)
                    self._show_session_waiting(target)

        self.push_screen(
            TextInputModal(
                title=f"Interrupt {session.name}",
                message_mode=True,
                scheme=self._cs,
            ),
            callback=_on_interrupt_dismiss,
        )


    @_safe_action
    def action_show_help(self: "IoMcpApp") -> None:
        """Show help screen with all keyboard shortcuts.

        Displays configurable key bindings and their descriptions.
        Press ? or Escape to return.
        """
        # Toggle off
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
        self._in_settings = True
        self._setting_edit_mode = False
        self._spawn_options = None
        self._quick_action_options = None
        self._help_mode = True

        s = self._cs
        kb = self._config.key_bindings if self._config else {}

        shortcuts = [
            (kb.get("cursorDown", "j") + "/" + kb.get("cursorUp", "k"), "Navigate choices up/down"),
            (kb.get("select", "enter"), "Select the highlighted choice"),
            ("1-9", "Instant select by number"),
            (kb.get("voiceInput", "space"), "Toggle voice recording for speech input"),
            (kb.get("freeformInput", "i"), "Type a freeform text reply"),
            (kb.get("queueMessage", "m"), "Queue a message for the agent"),
            (kb.get("settings", "s"), "Open the settings menu"),
            (kb.get("nextTab", "l") + "/" + kb.get("prevTab", "h"), "Switch between tabs"),
            (kb.get("nextChoicesTab", "n"), "Jump to next tab with active choices"),
            (kb.get("spawnAgent", "t"), "Spawn a new Claude Code agent"),
            (kb.get("multiSelect", "x"), "Enter/confirm multi-select mode"),
            (kb.get("conversationMode", "c"), "Toggle continuous voice conversation mode"),
            (kb.get("paneView", "v"), "Show live tmux pane output"),
            (kb.get("undoSelection", "u"), "Undo last selection"),
            (kb.get("dismiss", "d"), "Dismiss active choice without responding"),
            (kb.get("filterChoices", "slash"), "Filter choices by typing"),
            (kb.get("replayPrompt", "p"), "Replay the last prompt via TTS"),
            (kb.get("hotReload", "r"), "Refresh / hot reload"),
            (kb.get("quit", "q"), "Back / Quit (context-aware)"),
        ]

        preamble_widget = self.query_one("#preamble", Label)
        preamble_widget.update(
            f"[bold {s['accent']}]Keyboard Shortcuts[/bold {s['accent']}]  "
            f"[dim](?/esc to close)[/dim]"
        )
        preamble_widget.display = True

        # Ensure main content is visible, hide inbox pane in modal views
        self._ensure_main_content_visible(show_inbox=False)

        list_view = self.query_one("#choices", ListView)
        list_view.clear()

        for i, (key, desc) in enumerate(shortcuts):
            label = f"[bold {s['accent']}]{key:>12}[/bold {s['accent']}]  {desc}"
            list_view.append(ChoiceItem(label, "", index=i + 1, display_index=i))

        list_view.display = True
        list_view.index = 0
        list_view.focus()

        self._speak_ui(f"Help screen. {len(shortcuts)} shortcuts.")

    @_safe_action
    def action_view_system_logs(self: "IoMcpApp") -> None:
        """Show system logs: TUI errors, proxy logs, and speech history.

        Reads from /tmp/io-mcp-tui-error.log, /tmp/io-mcp-proxy.log,
        and the focused session's speech log. Displays entries in a
        scrollable list. Press Enter or Escape to return.
        """
        # Toggle off if already in system logs mode
        if getattr(self, '_system_logs_mode', False):
            self._system_logs_mode = False
            self._exit_settings()
            return

        session = self._focused()
        if session and (session.input_mode or session.voice_recording):
            return
        if self._in_settings or self._filter_mode:
            return

        self._tts.stop()

        # Collect logs from all sources
        log_entries = []  # (section, text) tuples
        flat_entries = []  # plain text for TTS on scroll

        # TUI error log
        tui_errors = read_log_tail(TUI_ERROR_LOG, 50)

        # Proxy log
        proxy_lines = read_log_tail(PROXY_LOG, 30)

        # Speech log from focused session
        speech_lines = []
        if session:
            import time as _time
            now = _time.time()
            for entry in session.speech_log[-30:]:
                elapsed = now - entry.timestamp
                if elapsed < 60:
                    age = f"{int(elapsed)}s ago"
                elif elapsed < 3600:
                    age = f"{int(elapsed)//60}m ago"
                else:
                    age = f"{int(elapsed)//3600}h ago"
                speech_lines.append((age, entry.text[:200]))

        # Build display entries
        s = self._cs

        if tui_errors:
            log_entries.append(("header", "TUI Errors", f"{len(tui_errors)} lines"))
            for line in tui_errors:
                log_entries.append(("tui_error", line.strip(), ""))
                flat_entries.append(line.strip())
        else:
            log_entries.append(("header", "TUI Errors", "none"))

        if proxy_lines:
            log_entries.append(("header", "Proxy Log", f"{len(proxy_lines)} lines"))
            for line in proxy_lines:
                log_entries.append(("proxy", line.strip(), ""))
                flat_entries.append(line.strip())
        else:
            log_entries.append(("header", "Proxy Log", "none"))

        if speech_lines:
            log_entries.append(("header", "Speech History", f"{len(speech_lines)} entries"))
            for age, text in speech_lines:
                log_entries.append(("speech", text, age))
                flat_entries.append(text)
        else:
            log_entries.append(("header", "Speech History", "none"))

        # Store flat entries for TTS on scroll
        self._system_log_entries = []
        self._system_log_full_entries = []  # full text for expanded view on scroll

        # Enter system logs mode (uses settings infrastructure for modal display)
        self._in_settings = True
        self._setting_edit_mode = False
        self._spawn_options = None
        self._quick_action_options = None
        self._system_logs_mode = True
        self._help_mode = False

        preamble_widget = self.query_one("#preamble", Label)
        total = len(tui_errors) + len(proxy_lines) + len(speech_lines)
        preamble_widget.update(
            f"[bold {s['accent']}]System Logs[/bold {s['accent']}] — "
            f"{total} entr{'y' if total == 1 else 'ies'}  "
            f"[dim](enter/esc to close)[/dim]"
        )
        preamble_widget.display = True

        # Ensure main content is visible, hide inbox pane in modal views
        self._ensure_main_content_visible(show_inbox=False)

        list_view = self.query_one("#choices", ListView)
        list_view.clear()

        display_idx = 0
        for entry_type, text, detail in log_entries:
            if entry_type == "header":
                # Section header — bold with accent color
                label = f"[bold {s['accent']}]━━ {text}[/bold {s['accent']}]"
                summary = f"[dim]{detail}[/dim]" if detail else ""
                list_view.append(ChoiceItem(label, summary, index=display_idx + 1, display_index=display_idx))
                self._system_log_entries.append(f"{text}: {detail}")
                self._system_log_full_entries.append(f"{text}: {detail}")
            elif entry_type == "tui_error":
                # Error lines — use error color for "---" delimiters
                if text.startswith("---"):
                    label = f"[{s['error']}]{text}[/{s['error']}]"
                else:
                    label = f"[{s['fg_dim']}]{text[:120]}[/{s['fg_dim']}]"
                list_view.append(ChoiceItem(label, "", index=display_idx + 1, display_index=display_idx))
                self._system_log_entries.append(text[:120])
                self._system_log_full_entries.append(text)
            elif entry_type == "proxy":
                label = f"[{s['fg_dim']}]{text[:120]}[/{s['fg_dim']}]"
                list_view.append(ChoiceItem(label, "", index=display_idx + 1, display_index=display_idx))
                self._system_log_entries.append(text[:120])
                self._system_log_full_entries.append(text)
            elif entry_type == "speech":
                label = f"[{s['blue']}]>[/{s['blue']}] {text[:100]}"
                summary = f"[{s['fg_dim']}]{detail}[/{s['fg_dim']}]" if detail else ""
                list_view.append(ChoiceItem(label, summary, index=display_idx + 1, display_index=display_idx))
                self._system_log_entries.append(text[:100])
                self._system_log_full_entries.append(text)
            display_idx += 1

        list_view.display = True
        list_view.index = 0
        list_view.focus()

        # Narrate summary
        parts = []
        if tui_errors:
            parts.append(f"{len(tui_errors)} TUI errors")
        if proxy_lines:
            parts.append(f"{len(proxy_lines)} proxy log lines")
        if speech_lines:
            parts.append(f"{len(speech_lines)} speech entries")
        summary = ", ".join(parts) if parts else "No logs found"
        self._speak_ui(f"System logs. {summary}.")
