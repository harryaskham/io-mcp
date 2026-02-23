"""MCP server module for io-mcp.

Defines the MCP tools and server setup, decoupled from the frontend.
The server communicates with any frontend that implements the Frontend protocol.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, Optional, Protocol

from mcp.server.fastmcp import FastMCP, Context

log = logging.getLogger("io-mcp.server")


class TTSBackend(Protocol):
    """Protocol for TTS backends (terminal paplay, Android native, etc.).

    Both the terminal-based TTSEngine and Android's TextToSpeech can
    implement this interface for platform-independent speech.
    """

    def speak(self, text: str, voice_override: Optional[str] = None,
              emotion_override: Optional[str] = None) -> None:
        """Speak text and BLOCK until playback finishes."""
        ...

    def speak_async(self, text: str, voice_override: Optional[str] = None,
                    emotion_override: Optional[str] = None) -> None:
        """Speak text without blocking."""
        ...

    def stop(self) -> None:
        """Kill any in-progress playback."""
        ...

    def clear_cache(self) -> None:
        """Remove cached audio files (no-op for native TTS)."""
        ...

    def pregenerate(self, texts: list[str]) -> None:
        """Pre-generate audio for texts (no-op for native TTS)."""
        ...


class Frontend(Protocol):
    """Protocol that frontends (TUI, Android, etc.) must implement.

    The MCP server calls these methods to interact with the user.
    All methods must be thread-safe.
    """

    @property
    def manager(self) -> Any:
        """SessionManager instance."""
        ...

    @property
    def config(self) -> Any:
        """IoMcpConfig instance (or None)."""
        ...

    @property
    def tts(self) -> Any:
        """TTSEngine instance."""
        ...

    def present_choices(self, session: Any, preamble: str, choices: list[dict]) -> dict:
        """Show choices and block until user selects. Returns selection dict."""
        ...

    def present_multi_select(self, session: Any, preamble: str, choices: list[dict]) -> list[dict]:
        """Show choices with checkboxes. Returns list of selected items."""
        ...

    def session_speak(self, session: Any, text: str, block: bool = True,
                      priority: int = 0, emotion: str = "") -> None:
        """Speak text for a session."""
        ...

    def session_speak_async(self, session: Any, text: str) -> None:
        """Non-blocking speak for a session."""
        ...

    def on_session_created(self, session: Any) -> None:
        """Called when a new session is created."""
        ...

    def update_tab_bar(self) -> None:
        """Update the tab/session display."""
        ...

    def hot_reload(self) -> None:
        """Trigger a hot reload of code and config."""
        ...


def _get_session_id(ctx: Context) -> str:
    """Extract a stable session ID from the MCP context."""
    session = ctx.session
    sid = getattr(session, "mcp_session_id", None)
    if sid:
        return str(sid)
    return str(id(session))


def create_mcp_server(
    frontend: Frontend,
    host: str = "0.0.0.0",
    port: int = 8444,
    append_options: list[str] | None = None,
    append_silent_options: list[str] | None = None,
) -> FastMCP:
    """Create and configure the MCP server with all tools.

    Args:
        frontend: Frontend implementation (TUI, Android, etc.)
        host: Server bind address
        port: Server port
        append_options: Extra choice options to always append
        append_silent_options: Extra silent options to always append

    Returns:
        Configured FastMCP server ready to run.
    """
    server = FastMCP("io-mcp", host=host, port=port)
    _append = append_options or []
    _append_silent = append_silent_options or []

    # Build config-based extra options
    _config_extras: list[dict] = []
    if frontend.config:
        for opt in frontend.config.extra_options:
            _config_extras.append({
                "label": opt.get("title", ""),
                "summary": opt.get("description", ""),
                "_silent": opt.get("silent", False),
            })

    def _drain_messages(session) -> list[str]:
        msgs = getattr(session, 'pending_messages', None)
        if msgs is None:
            return []
        drained = list(msgs)
        msgs.clear()
        return drained

    def _attach_messages(response: str, session) -> str:
        messages = _drain_messages(session)
        if not messages:
            return response
        try:
            data = json.loads(response)
            data["user_messages"] = messages
            return json.dumps(data)
        except (json.JSONDecodeError, TypeError):
            msg_text = "\n\n[User messages queued while you were working:\n" + "\n".join(f"- {m}" for m in messages) + "\n]"
            return response + msg_text

    def _safe_get_session(ctx: Context):
        import time as _time
        session_id = _get_session_id(ctx)
        session, created = frontend.manager.get_or_create(session_id)
        if created:
            try:
                frontend.on_session_created(session)
            except Exception:
                pass
        # Track tool call time for heartbeat/ambient mode
        session.last_tool_call = _time.time()
        session.heartbeat_spoken = False
        session.ambient_count = 0
        # Increment tool call counter
        session.tool_call_count = getattr(session, 'tool_call_count', 0) + 1
        # Reset health status — agent is active again
        if getattr(session, 'health_status', 'healthy') != 'healthy':
            session.health_status = 'healthy'
            session.health_alert_spoken = False
        return session

    def _registration_reminder(session) -> str:
        """Return a reminder string if session is not registered."""
        if not session.registered:
            return ("\n\n[REMINDER: Call register_session() first with your cwd, "
                    "hostname, tmux_session, and tmux_pane so io-mcp can manage "
                    "your session properly.]")
        return ""

    def _safe_tool(fn):
        import functools
        import traceback as tb

        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            try:
                # Track last tool name on session for visual progress
                ctx_arg = kwargs.get('ctx') or (args[-1] if args else None)
                if ctx_arg:
                    try:
                        session = _safe_get_session(ctx_arg)
                        session.last_tool_name = fn.__name__
                    except Exception:
                        pass
                return await fn(*args, **kwargs)
            except Exception as exc:
                err_msg = f"{type(exc).__name__}: {str(exc)[:200]}"
                log.error(f"Tool {fn.__name__} failed: {err_msg}")
                try:
                    with open("/tmp/io-mcp-tool-error.log", "a") as f:
                        f.write(f"\n--- {fn.__name__} ---\n{tb.format_exc()}\n")
                except Exception:
                    pass
                # Speak the error to the user so they know something went wrong
                try:
                    short_err = f"Tool error: {fn.__name__}. {str(exc)[:80]}"
                    frontend.tts.speak_async(short_err)
                except Exception:
                    pass
                return json.dumps({"error": err_msg, "tool": fn.__name__})
        return wrapper

    # ─── Tools ────────────────────────────────────────────────────────

    @server.tool()
    @_safe_tool
    async def present_choices(preamble: str, choices: list[dict], ctx: Context) -> str:
        """Present multi-choice options to the user via scroll-wheel TUI.

        The user navigates choices with a scroll wheel (or j/k keys) and
        hears each option read aloud via TTS. After dwelling for 5 seconds
        or pressing Enter, the selected choice is returned.

        Parameters
        ----------
        preamble:
            A brief 1-sentence summary spoken aloud before choices appear.
            Keep it concise — it is read via TTS through earphones.
        choices:
            List of choice objects, each with:
            - "label": Short 2-5 word label (read aloud on every scroll)
            - "summary": 1-2 sentence explanation (shown on screen)

        Returns
        -------
        str
            JSON string: {"selected": "chosen label", "summary": "chosen summary"}
        """
        if not choices:
            return json.dumps({"selected": "error", "summary": "No choices provided"})

        session = _safe_get_session(ctx)

        all_choices = list(choices)
        for opt in _append:
            if "::" in opt:
                title, desc = opt.split("::", 1)
            else:
                title, desc = opt, ""
            if not any(c.get("label", "").lower() == title.lower() for c in all_choices):
                all_choices.append({"label": title, "summary": desc})

        for opt in _append_silent:
            if "::" in opt:
                title, desc = opt.split("::", 1)
            else:
                title, desc = opt, ""
            if not any(c.get("label", "").lower() == title.lower() for c in all_choices):
                all_choices.append({"label": title, "summary": desc, "_silent": True})

        for opt in _config_extras:
            if not any(c.get("label", "").lower() == opt["label"].lower() for c in all_choices):
                all_choices.append(dict(opt))

        # Undo loop: re-present if user selects undo
        while True:
            # Save current choices for undo support
            session.last_preamble = preamble
            session.last_choices = list(all_choices)

            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(
                None, frontend.present_choices, session, preamble, all_choices
            )

            # Check for undo sentinel
            if result.get("selected") == "_undo":
                continue  # Re-present the same choices
            break

        return _attach_messages(json.dumps(result), session) + _registration_reminder(session)

    @server.tool()
    @_safe_tool
    async def present_multi_select(preamble: str, choices: list[dict], ctx: Context) -> str:
        """Present choices where the user can select multiple items.

        The user scrolls through options and toggles each with Enter.
        A "Done" option at the bottom submits all checked items.
        Use this for file picking, feature selection, or batch operations.

        Parameters
        ----------
        preamble:
            Brief summary spoken aloud before choices appear.
        choices:
            List of choice objects, each with:
            - "label": Short label for the option
            - "summary": Description (shown on screen)

        Returns
        -------
        str
            JSON string: {"selected": [{"label": "...", "summary": "..."}]}
        """
        if not choices:
            return json.dumps({"selected": []})

        session = _safe_get_session(ctx)

        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None, frontend.present_multi_select, session, preamble, list(choices)
        )
        return _attach_messages(json.dumps({"selected": result}), session)

    @server.tool()
    @_safe_tool
    async def speak(text: str, ctx: Context) -> str:
        """Speak text aloud via TTS through the user's earphones.

        Use this to narrate what you're doing — give short verbal status
        updates so the user can follow along without looking at the screen.

        Parameters
        ----------
        text:
            The text to speak. Keep it concise (1-2 sentences max).

        Returns
        -------
        str
            Confirmation message.
        """
        session = _safe_get_session(ctx)
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, frontend.session_speak, session, text, True, 0, "")
        preview = text[:100] + ("..." if len(text) > 100 else "")
        return _attach_messages(f"Spoke: {preview}", session)

    @server.tool()
    @_safe_tool
    async def speak_async(text: str, ctx: Context) -> str:
        """Speak text aloud via TTS WITHOUT blocking. Returns immediately.

        Use this for quick status updates where you don't need to wait
        for speech to finish before continuing work.

        Parameters
        ----------
        text:
            The text to speak. Keep it concise (1-2 sentences max).

        Returns
        -------
        str
            Confirmation message.
        """
        session = _safe_get_session(ctx)
        frontend.session_speak(session, text, False, 0, "")
        preview = text[:100] + ("..." if len(text) > 100 else "")
        return _attach_messages(f"Spoke: {preview}", session)

    @server.tool()
    @_safe_tool
    async def speak_urgent(text: str, ctx: Context) -> str:
        """Speak text with high priority, interrupting any current playback.

        Use this for important alerts or time-sensitive information that
        the user needs to hear immediately, even if other speech is playing.

        Parameters
        ----------
        text:
            The urgent text to speak. Keep it concise.

        Returns
        -------
        str
            Confirmation message.
        """
        session = _safe_get_session(ctx)
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, frontend.session_speak, session, text, True, 1, "")
        preview = text[:100] + ("..." if len(text) > 100 else "")
        return _attach_messages(f"Urgently spoke: {preview}", session)

    @server.tool()
    @_safe_tool
    async def set_speed(speed: float, ctx: Context) -> str:
        """Set the TTS playback speed.

        Parameters
        ----------
        speed:
            Speed multiplier (0.5 to 2.5). Higher = faster speech.

        Returns
        -------
        str
            Confirmation of the new speed setting.
        """
        if frontend.config:
            frontend.config.set_tts_speed(speed)
            frontend.config.save()
            frontend.tts.clear_cache()
        return f"Speed set to {speed}"

    @server.tool()
    @_safe_tool
    async def set_voice(voice: str, ctx: Context) -> str:
        """Set the TTS voice.

        Parameters
        ----------
        voice:
            Voice name. Available voices depend on the current TTS model.

        Returns
        -------
        str
            Confirmation of the new voice setting.
        """
        if frontend.config:
            frontend.config.set_tts_voice(voice)
            frontend.config.save()
            frontend.tts.clear_cache()
        return f"Voice set to {voice}"

    @server.tool()
    @_safe_tool
    async def set_tts_model(model: str, ctx: Context) -> str:
        """Set the TTS model. This also resets the voice to the new model's default.

        Parameters
        ----------
        model:
            Model name. Available: gpt-4o-mini-tts, mai-voice-1.

        Returns
        -------
        str
            Confirmation of the new model and voice.
        """
        if frontend.config:
            frontend.config.set_tts_model(model)
            frontend.config.save()
            frontend.tts.clear_cache()
            return f"TTS model set to {model}, voice reset to {frontend.config.tts_voice}"
        return f"TTS model set to {model}"

    @server.tool()
    @_safe_tool
    async def set_stt_model(model: str, ctx: Context) -> str:
        """Set the STT (speech-to-text) model.

        Parameters
        ----------
        model:
            Model name. Available: whisper, gpt-4o-mini-transcribe, mai-ears-1.

        Returns
        -------
        str
            Confirmation of the new STT model.
        """
        if frontend.config:
            frontend.config.set_stt_model(model)
            frontend.config.save()
        return f"STT model set to {model}"

    @server.tool()
    @_safe_tool
    async def set_emotion(emotion: str, ctx: Context) -> str:
        """Set the TTS emotion/voice style.

        Parameters
        ----------
        emotion:
            Preset name or custom instruction text.
            Presets: happy, calm, excited, serious, friendly, neutral, storyteller, gentle.

        Returns
        -------
        str
            Confirmation of the new emotion setting.
        """
        if frontend.config:
            frontend.config.set_tts_emotion(emotion)
            frontend.config.save()
            frontend.tts.clear_cache()
        return f"Emotion set to: {emotion}"

    @server.tool()
    @_safe_tool
    async def get_settings(ctx: Context) -> str:
        """Get the current io-mcp settings.

        Returns
        -------
        str
            JSON string with current TTS model, voice, speed, and STT model.
        """
        if frontend.config:
            return json.dumps({
                "tts_model": frontend.config.tts_model_name,
                "tts_voice": frontend.config.tts_voice,
                "tts_speed": frontend.config.tts_speed,
                "tts_emotion": frontend.config.tts_emotion,
                "tts_voice_options": frontend.config.tts_voice_options,
                "tts_models": frontend.config.tts_model_names,
                "emotion_presets": frontend.config.emotion_preset_names,
                "stt_model": frontend.config.stt_model_name,
                "stt_models": frontend.config.stt_model_names,
            })
        return json.dumps({"error": "No config available"})

    @server.tool()
    @_safe_tool
    async def register_session(
        ctx: Context,
        cwd: str = "",
        hostname: str = "",
        tmux_session: str = "",
        tmux_pane: str = "",
        name: str = "",
        voice: str = "",
        emotion: str = "",
        metadata: dict = {},
    ) -> str:
        """Register this agent session with io-mcp.

        MUST be called before using any other io-mcp tools. Provides
        metadata about the agent's environment so io-mcp can:
        - Display agent info in the dashboard
        - Control agents via tmux (send messages, restart)
        - Reconnect agents after io-mcp restarts

        Parameters
        ----------
        cwd:
            The agent's current working directory.
        hostname:
            The machine the agent is running on.
        tmux_session:
            The tmux session name (if running in tmux).
        tmux_pane:
            The tmux pane ID (e.g. "%42").
        name:
            A descriptive name for this session tab.
        voice:
            Preferred TTS voice for this session.
        emotion:
            Preferred TTS emotion for this session.
        metadata:
            Any additional key-value metadata.

        Returns
        -------
        str
            JSON confirmation with assigned session info.
        """
        session = _safe_get_session(ctx)
        session.registered = True
        if cwd:
            session.cwd = cwd
        if hostname:
            session.hostname = hostname
        if tmux_session:
            session.tmux_session = tmux_session
        if tmux_pane:
            session.tmux_pane = tmux_pane
        if name:
            session.name = name
        if voice:
            session.voice_override = voice
        if emotion:
            session.emotion_override = emotion
        if metadata:
            session.agent_metadata.update(metadata)

        # Detect if agent is on the same device as io-mcp
        import socket
        local_hostname = socket.gethostname()
        is_local = (hostname == local_hostname) if hostname else True

        try:
            frontend.update_tab_bar()
        except Exception:
            pass

        # Persist registered session metadata
        try:
            frontend.manager.save_registered()
        except Exception:
            pass

        return json.dumps({
            "status": "registered",
            "session_id": session.session_id,
            "name": session.name,
            "is_local": is_local,
            "io_mcp_hostname": local_hostname,
            "features": [
                "present_choices", "present_multi_select",
                "speak", "speak_async", "speak_urgent",
                "set_speed", "set_voice", "set_emotion",
                "set_tts_model", "set_stt_model",
                "rename_session", "get_settings", "reload_config",
                "pull_latest", "run_command",
            ],
        })

    @server.tool()
    @_safe_tool
    async def rename_session(name: str, ctx: Context) -> str:
        """Rename the current session tab.

        Parameters
        ----------
        name:
            The new tab name (e.g., "Code Review", "Tests", "Refactor").

        Returns
        -------
        str
            Confirmation of the new name.
        """
        session = _safe_get_session(ctx)
        session.name = name
        try:
            frontend.update_tab_bar()
        except Exception:
            pass
        return f"Session renamed to: {name}"

    @server.tool()
    @_safe_tool
    async def reload_config(ctx: Context) -> str:
        """Reload the io-mcp configuration from disk.

        Re-reads ~/.config/io-mcp/config.yml and any local .io-mcp.yml,
        then clears the TTS cache so new settings take effect immediately.

        Returns
        -------
        str
            Confirmation with the reloaded settings.
        """
        if frontend.config:
            frontend.config.reload()
            frontend.tts.clear_cache()
            return json.dumps({
                "status": "reloaded",
                "tts_model": frontend.config.tts_model_name,
                "tts_voice": frontend.config.tts_voice,
                "tts_speed": frontend.config.tts_speed,
                "tts_emotion": frontend.config.tts_emotion,
                "stt_model": frontend.config.stt_model_name,
            })
        return json.dumps({"status": "no config to reload"})

    @server.tool()
    @_safe_tool
    async def pull_latest(ctx: Context) -> str:
        """Pull the latest changes from the remote git repository.

        Runs `git pull --rebase origin main` in the io-mcp project directory,
        then triggers a hot reload if successful.

        Returns
        -------
        str
            The git output or error message.
        """
        import subprocess as sp
        project_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        try:
            result = sp.run(
                ["git", "pull", "--rebase", "origin", "main"],
                cwd=project_dir,
                capture_output=True,
                text=True,
                timeout=30,
            )
            output = result.stdout.strip()
            if result.returncode != 0:
                err = result.stderr.strip()
                return json.dumps({"status": "error", "output": output, "error": err})

            try:
                frontend.hot_reload()
                return json.dumps({"status": "pulled_and_reloaded", "output": output})
            except Exception:
                return json.dumps({"status": "pulled", "output": output, "note": "Hot reload failed"})

        except sp.TimeoutExpired:
            return json.dumps({"status": "error", "error": "Git pull timed out after 30s"})
        except Exception as e:
            return json.dumps({"status": "error", "error": str(e)})

    @server.tool()
    @_safe_tool
    async def run_command(command: str, ctx: Context) -> str:
        """Run a shell command on the device running the io-mcp server.

        The command is first presented to the user for confirmation via
        the TUI/frontend. If the user approves, it runs and returns the
        output. If denied, returns a rejection message.

        Use this for operations on the server device like checking status,
        installing packages, managing files, or running scripts.

        Parameters
        ----------
        command:
            The shell command to run (e.g., "git status", "ls -la", "uname -a").

        Returns
        -------
        str
            JSON with status, stdout, stderr, and return code.
        """
        import subprocess as sp

        session = _safe_get_session(ctx)

        # Present confirmation to user
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None,
            frontend.present_choices,
            session,
            f"Agent wants to run: {command}",
            [
                {"label": "Approve", "summary": f"Run: {command}"},
                {"label": "Deny", "summary": "Reject this command"},
            ],
        )

        selected = result.get("selected", "")
        if selected.lower() != "approve":
            return json.dumps({"status": "denied", "command": command})

        # Run the command
        try:
            proc = sp.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=60,
            )
            return json.dumps({
                "status": "completed",
                "command": command,
                "returncode": proc.returncode,
                "stdout": proc.stdout[:5000],  # limit output size
                "stderr": proc.stderr[:2000],
            })
        except sp.TimeoutExpired:
            return json.dumps({"status": "timeout", "command": command,
                             "error": "Command timed out after 60s"})
        except Exception as e:
            return json.dumps({"status": "error", "command": command,
                             "error": str(e)})

    return server
