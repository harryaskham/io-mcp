"""View action mixins for IoMcpApp.

Contains dashboard, timeline (agent log), pane view, and help screen
action methods. These are mixed into IoMcpApp via multiple inheritance.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
from typing import TYPE_CHECKING

from textual.widgets import Label, ListView, RichLog

from .themes import DEFAULT_SCHEME, get_scheme
from .widgets import ChoiceItem, _safe_action

if TYPE_CHECKING:
    from .app import IoMcpApp


class ViewsMixin:
    """Mixin providing dashboard, timeline, pane view, and help screen actions."""

    @_safe_action
    def action_dashboard(self: "IoMcpApp") -> None:
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
            self._speak_ui("No active agents")
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

        now = time.time()
        narration_parts = [f"{len(sessions)} agent{'s' if len(sessions) != 1 else ''} active."]

        for i, sess in enumerate(sessions):
            # Determine health status for visual indicator
            health = getattr(sess, 'health_status', 'healthy')

            # Determine activity status
            if sess.active:
                status_text = f"[{s['success']}]o choices[/{s['success']}]"
                status_narr = "has choices"
            elif sess.voice_recording:
                status_text = f"[{s['error']}]o recording[/{s['error']}]"
                status_narr = "recording"
            elif health == "unresponsive":
                status_text = f"[{s['error']}]x unresponsive[/{s['error']}]"
                status_narr = "unresponsive"
            elif health == "warning":
                status_text = f"[{s['warning']}]! stuck?[/{s['warning']}]"
                status_narr = "may be stuck"
            else:
                status_text = f"[{s['warning']}]- working[/{s['warning']}]"
                status_narr = "working"

            # Elapsed time
            elapsed = now - getattr(sess, 'last_tool_call', now)
            if elapsed < 60:
                time_str = f"{int(elapsed)}s"
            else:
                time_str = f"{int(elapsed)//60}m{int(elapsed)%60:02d}s"

            # Tool call stats
            tool_count = getattr(sess, 'tool_call_count', 0)
            n_selections = len(sess.history)
            stats_parts = []
            if tool_count > 0:
                stats_parts.append(f"{tool_count} calls")
            if n_selections > 0:
                stats_parts.append(f"{n_selections} sel")
            stats_str = f" [{s['fg_dim']}]({', '.join(stats_parts)})[/{s['fg_dim']}]" if stats_parts else ""

            # Pending messages
            msgs = getattr(sess, 'pending_messages', [])
            msg_info = f" [{s['purple']}]{len(msgs)} msg[/{s['purple']}]" if msgs else ""

            # Tmux info
            tmux_pane = getattr(sess, 'tmux_pane', '')
            pane_info = f" [{s['fg_dim']}]{tmux_pane}[/{s['fg_dim']}]" if tmux_pane else ""

            label = f"{sess.name}  {status_text}  [{s['fg_dim']}]{time_str}[/{s['fg_dim']}]{stats_str}{msg_info}{pane_info}"

            # Smart summary
            try:
                summary_text = sess.summary()
            except Exception:
                summary_text = ""
            if not summary_text:
                if sess.speech_log:
                    summary_text = sess.speech_log[-1].text
                else:
                    summary_text = "[dim]no activity[/dim]"

            list_view.append(ChoiceItem(label, summary_text, index=i + 1, display_index=i))
            narration_parts.append(f"{sess.name}: {status_narr}, {time_str}.")

        list_view.display = True
        list_view.index = 0
        list_view.focus()

        # Narrate the dashboard
        self._speak_ui(" ".join(narration_parts))

    def _dashboard_session_actions(self: "IoMcpApp", session_idx: int) -> None:
        """Show actions for a selected dashboard session.

        Presents a sub-menu with options to switch to, close, or kill
        the selected session's tmux pane.
        """
        sessions = self.manager.all_sessions()
        if session_idx >= len(sessions):
            self._exit_settings()
            return

        target = sessions[session_idx]
        self._dashboard_action_target = target
        self._dashboard_action_mode = True
        self._dashboard_mode = False

        s = getattr(self, '_cs', get_scheme(DEFAULT_SCHEME))

        preamble_widget = self.query_one("#preamble", Label)
        preamble_widget.update(
            f"[bold {s['accent']}]{target.name}[/bold {s['accent']}]  "
            f"[dim](select action)[/dim]"
        )

        actions = [
            {"label": "Switch to", "summary": f"Focus on {target.name}"},
            {"label": "Close tab", "summary": "Remove this session from the TUI"},
        ]

        # Only show kill tmux if the session has a tmux pane
        pane = getattr(target, 'tmux_pane', '')
        if pane:
            actions.append({"label": "Kill tmux pane", "summary": f"Kill pane {pane} and close tab"})

        actions.append({"label": "Back", "summary": "Return to dashboard"})

        list_view = self.query_one("#choices", ListView)
        list_view.clear()
        for i, action in enumerate(actions):
            list_view.append(ChoiceItem(
                action["label"], action["summary"],
                index=i + 1, display_index=i,
            ))
        list_view.display = True
        list_view.index = 0
        list_view.focus()

        self._speak_ui(f"{target.name}. Switch, close, or kill.")

    def _handle_dashboard_action(self: "IoMcpApp", action_idx: int) -> None:
        """Handle a selection from the dashboard session actions menu."""
        target = getattr(self, '_dashboard_action_target', None)
        self._dashboard_action_mode = False
        self._dashboard_action_target = None

        if not target:
            self._exit_settings()
            return

        # Verify the session still exists (could have been removed by health monitor)
        if not self.manager.get(target.session_id):
            self._speak_ui("Session no longer exists")
            if self.manager.count() > 0:
                self.action_dashboard()
            else:
                self._exit_settings()
            return

        pane = getattr(target, 'tmux_pane', '')
        has_pane = bool(pane)

        # Map index to action, accounting for optional "Kill tmux pane"
        actions = ["switch", "close"]
        if has_pane:
            actions.append("kill")
        actions.append("back")

        if action_idx >= len(actions):
            self._exit_settings()
            return

        action = actions[action_idx]

        if action == "switch":
            self._in_settings = False
            self._switch_to_session(target)
        elif action == "close":
            self._close_session(target)
        elif action == "kill":
            self._kill_session(target)
        else:
            # Back — return to dashboard
            self.action_dashboard()

    def _close_session(self: "IoMcpApp", session) -> None:
        """Close a session tab without killing the tmux pane."""
        name = session.name
        self.on_session_removed(session.session_id)
        self._speak_ui(f"Closed {name}")
        # Return to dashboard if sessions remain, otherwise exit
        if self.manager.count() > 0:
            self.action_dashboard()
        else:
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
        if self.manager.count() > 0:
            self.action_dashboard()
        else:
            self._exit_settings()

    @_safe_action
    def action_agent_log(self: "IoMcpApp") -> None:
        """Show a unified timeline of agent activity.

        Merges speech entries and selection history into a chronological
        timeline. Each entry shows type (speech/selection), age, and text.
        Narrates entries when highlighted. Press g or Escape to return.
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
            self._speak_ui("No active session")
            return

        # Build timeline from speech log + history
        timeline = session.timeline(max_entries=50)
        if not timeline:
            self._speak_ui("No activity log for this session")
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

        # Show summary at top
        summary_text = ""
        try:
            summary_text = session.summary()
        except Exception:
            pass

        preamble_widget = self.query_one("#preamble", Label)
        count = len(timeline)
        preamble_parts = [
            f"[bold {s['accent']}]Timeline[/bold {s['accent']}]",
            f"[{s['fg_dim']}]{session.name}[/{s['fg_dim']}]",
            f"{count} entr{'y' if count == 1 else 'ies'}",
        ]
        if summary_text:
            preamble_parts.append(f"[dim]{summary_text}[/dim]")
        preamble_parts.append("[dim](g/esc to close)[/dim]")
        preamble_widget.update(" — ".join(preamble_parts))
        preamble_widget.display = True

        list_view = self.query_one("#choices", ListView)
        list_view.clear()

        for i, entry in enumerate(timeline):
            age = entry.get("age", "")
            entry_type = entry.get("type", "speech")
            text = entry.get("text", "")
            detail = entry.get("detail", "")

            display_text = text

            # Type indicator
            if entry_type == "selection":
                type_mark = f"[{s['success']}]*[/{s['success']}]"
            else:
                type_mark = f"[{s['blue']}]>[/{s['blue']}]"

            label = f"{type_mark} [{s['fg_dim']}]{age}[/{s['fg_dim']}]  {display_text}"
            summary = detail if detail else ""

            list_view.append(ChoiceItem(label, summary, index=i + 1, display_index=i))

        list_view.display = True
        # Start at the top (most recent, since timeline is sorted desc)
        list_view.index = 0
        list_view.focus()

        self._speak_ui(f"Timeline. {count} entries. Most recent shown.")

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

        # Show pane view, hide choices
        self.query_one("#choices").display = False
        self.query_one("#preamble").display = False
        pane_view.clear()
        pane_view.display = True

        def _refresh_pane():
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

        # Initial refresh
        threading.Thread(target=_refresh_pane, daemon=True).start()

        # Auto-refresh every 2 seconds
        self._pane_refresh_timer = self.set_interval(2.0, lambda: threading.Thread(
            target=_refresh_pane, daemon=True).start())

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
        self._dashboard_mode = False
        self._log_viewer_mode = False
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
            (kb.get("nextTab", "l") + "/" + kb.get("prevTab", "h"), "Switch between agent tabs"),
            (kb.get("nextChoicesTab", "n"), "Jump to next tab with active choices"),
            (kb.get("spawnAgent", "t"), "Spawn a new Claude Code agent"),
            (kb.get("multiSelect", "x"), "Enter/confirm multi-select mode"),
            (kb.get("conversationMode", "c"), "Toggle continuous voice conversation mode"),
            (kb.get("dashboard", "d"), "Show dashboard overview of all agents"),
            (kb.get("paneView", "v"), "Show live tmux pane output"),
            (kb.get("agentLog", "g"), "Show timeline / agent log"),
            (kb.get("undoSelection", "u"), "Undo last selection"),
            (kb.get("filterChoices", "slash"), "Filter choices by typing"),
            (kb.get("replayPrompt", "p"), "Replay the last prompt via TTS"),
            (kb.get("hotReload", "r"), "Hot reload the TUI code"),
            (kb.get("quit", "q"), "Back / Quit (context-aware)"),
        ]

        preamble_widget = self.query_one("#preamble", Label)
        preamble_widget.update(
            f"[bold {s['accent']}]Keyboard Shortcuts[/bold {s['accent']}]  "
            f"[dim](?/esc to close)[/dim]"
        )
        preamble_widget.display = True

        list_view = self.query_one("#choices", ListView)
        list_view.clear()

        for i, (key, desc) in enumerate(shortcuts):
            label = f"[bold {s['accent']}]{key:>12}[/bold {s['accent']}]  {desc}"
            list_view.append(ChoiceItem(label, "", index=i + 1, display_index=i))

        list_view.display = True
        list_view.index = 0
        list_view.focus()

        self._speak_ui(f"Help screen. {len(shortcuts)} shortcuts.")
