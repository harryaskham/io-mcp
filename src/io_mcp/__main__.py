"""
io-mcp — MCP server for agent I/O via scroll-wheel and TTS.

Exposes MCP tools via streamable-http transport on port 8444:

  present_choices(preamble, choices)
      Show choices in the TUI, block until user scrolls and selects.
      Returns JSON: {"selected": "label", "summary": "..."}.

  speak(text)
      Blocking TTS narration through earphones.

  speak_async(text)
      Non-blocking TTS narration.

Multiple agents can connect simultaneously — each gets a session tab.

Textual TUI runs in the main thread (needs signal handlers).
MCP streamable-http server runs in a background thread via uvicorn.

Usage:
    cd ~/cosmos/projects/io-mcp && uv run io-mcp
    # or: uv run io-mcp --local   (use espeak-ng instead of gpt-4o-mini-tts)
    # or: uv run io-mcp --port 9000
    # or: uv run io-mcp --dwell=5  (auto-select after 5s)
"""

from __future__ import annotations

import argparse
import asyncio
import atexit
import json
import logging
import os
import signal
import socket
import threading
import sys

from mcp.server.fastmcp import FastMCP, Context

from .config import IoMcpConfig
from .tui import IoMcpApp
from .tts import TTSEngine

log = logging.getLogger("io_mcp")

PID_FILE = "/tmp/io-mcp.pid"


def _write_pid_file() -> None:
    """Write PID file so hooks can detect io-mcp is running."""
    with open(PID_FILE, "w") as f:
        f.write(str(os.getpid()))


def _remove_pid_file() -> None:
    """Remove PID file on exit."""
    try:
        os.unlink(PID_FILE)
    except OSError:
        pass


def _kill_existing_instance() -> None:
    """Kill any previous io-mcp instance so we can rebind the port cleanly.

    This ensures that after a restart, the HTTP port is immediately available
    for new agent connections.
    """
    try:
        with open(PID_FILE, "r") as f:
            old_pid = int(f.read().strip())
        if old_pid != os.getpid():
            os.kill(old_pid, signal.SIGTERM)
            import time
            time.sleep(0.3)  # Give it a moment to die
            try:
                os.kill(old_pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
    except (FileNotFoundError, ValueError, ProcessLookupError, PermissionError):
        pass


def _get_session_id(ctx: Context) -> str:
    """Extract a stable session ID from the MCP context.

    For streamable-http: uses the mcp_session_id (a UUID string).
    Fallback: uses id(ctx.session) as a string.
    """
    session = ctx.session
    # streamable-http transport has mcp_session_id
    sid = getattr(session, "mcp_session_id", None)
    if sid:
        return str(sid)
    return str(id(session))



def _run_mcp_server(app: IoMcpApp, host: str, port: int,
                    append_options: list[str] | None = None,
                    append_silent_options: list[str] | None = None) -> None:
    """Run the MCP streamable-http server in a background thread."""
    try:
        _run_mcp_server_inner(app, host, port, append_options, append_silent_options)
    except Exception:
        import traceback
        crash = traceback.format_exc()
        # Write to file since Textual captures stderr
        with open("/tmp/io-mcp-crash.log", "w") as f:
            f.write(crash)
        log.error("MCP server thread crashed — see /tmp/io-mcp-crash.log")


def _run_mcp_server_inner(app: IoMcpApp, host: str, port: int,
                          append_options: list[str] | None = None,
                          append_silent_options: list[str] | None = None) -> None:
    """Inner implementation of MCP server startup."""

    with open("/tmp/io-mcp-server.log", "w") as f:
        f.write(f"Starting MCP server on {host}:{port}\n")
        f.flush()

    server = FastMCP("io-mcp", host=host, port=port)
    _append = append_options or []
    _append_silent = append_silent_options or []

    # Build config-based extra options
    _config_extras: list[dict] = []
    if app._config:
        for opt in app._config.extra_options:
            _config_extras.append({
                "label": opt.get("title", ""),
                "summary": opt.get("description", ""),
                "_silent": opt.get("silent", False),
            })

    def _drain_messages(session) -> list[str]:
        """Drain and return any queued user messages for a session."""
        msgs = getattr(session, 'pending_messages', None)
        if msgs is None:
            return []
        drained = list(msgs)
        msgs.clear()
        return drained

    def _attach_messages(response: str, session) -> str:
        """If there are queued user messages, attach them to the JSON response."""
        messages = _drain_messages(session)
        if not messages:
            return response
        try:
            data = json.loads(response)
            data["user_messages"] = messages
            return json.dumps(data)
        except (json.JSONDecodeError, TypeError):
            # Non-JSON response, append as text
            msg_text = "\n\n[User messages queued while you were working:\n" + "\n".join(f"- {m}" for m in messages) + "\n]"
            return response + msg_text

    @server.tool()
    async def present_choices(
        preamble: str,
        choices: list[dict],
        ctx: Context,
    ) -> str:
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

        # Get or create session for this MCP client
        session_id = _get_session_id(ctx)
        session, created = app.manager.get_or_create(session_id)
        if created:
            app.on_session_created(session)

        # Append persistent options from --append-option flags
        all_choices = list(choices)
        for opt in _append:
            # Parse "title::description" format
            if "::" in opt:
                title, desc = opt.split("::", 1)
            else:
                title, desc = opt, ""
            # Don't duplicate if Claude already included it
            if not any(c.get("label", "").lower() == title.lower() for c in all_choices):
                all_choices.append({"label": title, "summary": desc})

        # Append silent options from --append-silent-option flags
        for opt in _append_silent:
            if "::" in opt:
                title, desc = opt.split("::", 1)
            else:
                title, desc = opt, ""
            if not any(c.get("label", "").lower() == title.lower() for c in all_choices):
                all_choices.append({"label": title, "summary": desc, "_silent": True})

        # Append config-based extra options
        for opt in _config_extras:
            if not any(c.get("label", "").lower() == opt["label"].lower() for c in all_choices):
                all_choices.append(dict(opt))

        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None, app.present_choices, session, preamble, all_choices
        )

        return _attach_messages(json.dumps(result), session)

    @server.tool()
    async def speak(text: str, ctx: Context) -> str:
        """Speak text aloud via TTS through the user's earphones.

        Use this to narrate what you're doing — give short verbal status
        updates so the user can follow along without looking at the screen.

        Examples:
            speak("Reading the config file")
            speak("Found three test failures, fixing now")
            speak("Done. Ready for your next choice.")

        Parameters
        ----------
        text:
            The text to speak. Keep it concise (1-2 sentences max).

        Returns
        -------
        str
            Confirmation message.
        """
        # Get or create session for this MCP client
        session_id = _get_session_id(ctx)
        session, created = app.manager.get_or_create(session_id)
        if created:
            app.on_session_created(session)

        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, app.session_speak, session, text, True)
        preview = text[:100] + ("..." if len(text) > 100 else "")
        return _attach_messages(f"Spoke: {preview}", session)

    @server.tool()
    async def speak_async(text: str, ctx: Context) -> str:
        """Speak text aloud via TTS WITHOUT blocking. Returns immediately.

        Use this for quick status updates where you don't need to wait
        for speech to finish before continuing work. Prefer this over
        speak() for brief narration between tool calls.

        The audio plays in the background while you continue working.
        If new speech is requested before the current one finishes,
        the current playback is interrupted.

        Parameters
        ----------
        text:
            The text to speak. Keep it concise (1-2 sentences max).

        Returns
        -------
        str
            Confirmation message.
        """
        # Get or create session for this MCP client
        session_id = _get_session_id(ctx)
        session, created = app.manager.get_or_create(session_id)
        if created:
            app.on_session_created(session)

        app.session_speak_async(session, text)
        preview = text[:100] + ("..." if len(text) > 100 else "")
        return _attach_messages(f"Spoke: {preview}", session)

    @server.tool()
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
        session_id = _get_session_id(ctx)
        session, created = app.manager.get_or_create(session_id)
        if created:
            app.on_session_created(session)

        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, app.session_speak, session, text, True, 1)
        preview = text[:100] + ("..." if len(text) > 100 else "")
        return _attach_messages(f"Urgently spoke: {preview}", session)

    @server.tool()
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
        if app._config:
            app._config.set_tts_speed(speed)
            app._config.save()
            app._tts.clear_cache()
            return f"Speed set to {speed}"
        return "No config available"

    @server.tool()
    async def set_voice(voice: str, ctx: Context) -> str:
        """Set the TTS voice.

        Parameters
        ----------
        voice:
            Voice name. Available voices depend on the current TTS model.
            For gpt-4o-mini-tts: alloy, ash, ballad, coral, echo, fable, onyx, nova, sage, shimmer, verse.
            For mai-voice-1: en-US-Noa:MAI-Voice-1, en-US-Teo:MAI-Voice-1.

        Returns
        -------
        str
            Confirmation of the new voice setting.
        """
        if app._config:
            app._config.set_tts_voice(voice)
            app._config.save()
            app._tts.clear_cache()
            return f"Voice set to {voice}"
        return "No config available"

    @server.tool()
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
        if app._config:
            app._config.set_tts_model(model)
            app._config.save()
            app._tts.clear_cache()
            return f"TTS model set to {model}, voice reset to {app._config.tts_voice}"
        return "No config available"

    @server.tool()
    async def set_emotion(emotion: str, ctx: Context) -> str:
        """Set the TTS emotion/voice style.

        Uses emotion presets that map to voice instructions. The emotion
        affects how the TTS voice sounds — its tone, energy, and warmth.

        Parameters
        ----------
        emotion:
            Preset name or custom instruction text.
            Presets: happy, calm, excited, serious, friendly, neutral, storyteller, gentle.
            Or pass any custom instruction string for the voice style.

        Returns
        -------
        str
            Confirmation of the new emotion setting.
        """
        if app._config:
            app._config.set_tts_emotion(emotion)
            app._config.save()
            app._tts.clear_cache()
            instructions = app._config.tts_instructions
            return f"Emotion set to '{emotion}': {instructions[:80]}"
        return "No config available"

    @server.tool()
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
        if app._config:
            app._config.set_stt_model(model)
            app._config.save()
            return f"STT model set to {model}"
        return "No config available"

    @server.tool()
    async def get_settings(ctx: Context) -> str:
        """Get the current io-mcp settings.

        Returns
        -------
        str
            JSON string with current TTS model, voice, speed, and STT model.
        """
        if app._config:
            return json.dumps({
                "tts_model": app._config.tts_model_name,
                "tts_voice": app._config.tts_voice,
                "tts_speed": app._config.tts_speed,
                "tts_emotion": app._config.tts_emotion,
                "tts_voice_options": app._config.tts_voice_options,
                "tts_models": app._config.tts_model_names,
                "emotion_presets": app._config.emotion_preset_names,
                "stt_model": app._config.stt_model_name,
                "stt_models": app._config.stt_model_names,
            })
        return json.dumps({"error": "No config available"})

    @server.tool()
    async def rename_session(name: str, ctx: Context) -> str:
        """Rename the current session tab.

        Sets a descriptive name for this agent's tab in the TUI,
        replacing the default "Agent N" label.

        Parameters
        ----------
        name:
            The new tab name (e.g., "Code Review", "Tests", "Refactor").

        Returns
        -------
        str
            Confirmation of the new name.
        """
        session_id = _get_session_id(ctx)
        session, created = app.manager.get_or_create(session_id)
        if created:
            app.on_session_created(session)
        session.name = name
        try:
            app.call_from_thread(app._update_tab_bar)
        except Exception:
            pass
        return f"Session renamed to: {name}"

    @server.tool()
    async def reload_config(ctx: Context) -> str:
        """Reload the io-mcp configuration from disk.

        Re-reads ~/.config/io-mcp/config.yml and any local .io-mcp.yml,
        then clears the TTS cache so new settings take effect immediately.

        Returns
        -------
        str
            Confirmation with the reloaded settings.
        """
        if app._config:
            app._config.reload()
            app._tts.clear_cache()
            return json.dumps({
                "status": "reloaded",
                "tts_model": app._config.tts_model_name,
                "tts_voice": app._config.tts_voice,
                "tts_speed": app._config.tts_speed,
                "stt_model": app._config.stt_model_name,
            })
        return json.dumps({"error": "No config available"})

    @server.tool()
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
        # Find the project root (where .git is)
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

            # Trigger hot reload if available
            try:
                app.call_from_thread(app.action_hot_reload)
                return json.dumps({"status": "pulled_and_reloaded", "output": output})
            except Exception:
                return json.dumps({"status": "pulled", "output": output, "note": "Hot reload failed — restart io-mcp for changes to take effect"})

        except sp.TimeoutExpired:
            return json.dumps({"status": "error", "error": "Git pull timed out after 30s"})
        except Exception as e:
            return json.dumps({"status": "error", "error": str(e)})

    # Run streamable-http server (blocks this thread)
    # Log to file since Textual captures stdout/stderr
    _log = logging.getLogger("uvicorn")
    _log.handlers.clear()
    _fh = logging.FileHandler("/tmp/io-mcp-server.log", mode="a")
    _fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    _log.addHandler(_fh)
    _log.setLevel(logging.DEBUG)

    with open("/tmp/io-mcp-server.log", "a") as f:
        f.write("Calling server.run(transport='streamable-http')\n")

    server.run(transport="streamable-http")

    with open("/tmp/io-mcp-server.log", "a") as f:
        f.write("server.run() returned (should not happen)\n")


def _acquire_wake_lock() -> None:
    """Acquire Android wake lock via termux-exec to prevent the device
    from sleeping and killing the process. Only works on Nix-on-Droid.

    For keeping the screen on, users should enable:
      Settings → Developer options → Stay awake (while charging)
    or use `termux-exec settings put system screen_off_timeout 2147483647`
    """
    import shutil
    termux_exec = shutil.which("termux-exec")
    if not termux_exec:
        return
    try:
        import subprocess
        subprocess.Popen(
            [termux_exec, "termux-wake-lock"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        print("  Wake lock: acquired", flush=True)
    except Exception:
        pass


def _release_wake_lock() -> None:
    """Release Android wake lock."""
    import shutil
    termux_exec = shutil.which("termux-exec")
    if not termux_exec:
        return
    try:
        import subprocess
        subprocess.run(
            [termux_exec, "termux-wake-unlock"],
            timeout=3, capture_output=True,
        )
    except Exception:
        pass


def main() -> None:
    parser = argparse.ArgumentParser(
        description="io-mcp — scroll-wheel input + TTS narration MCP server"
    )
    parser.add_argument(
        "--local", action="store_true",
        help="Use espeak-ng (local, fast) instead of gpt-4o-mini-tts (API)"
    )
    parser.add_argument(
        "--port", type=int, default=8444,
        help="Server port (default: 8444)"
    )
    parser.add_argument(
        "--host", default="0.0.0.0",
        help="Server bind address (default: 0.0.0.0)"
    )
    parser.add_argument(
        "--dwell", type=float, default=0.0, metavar="SECONDS",
        help="Enable dwell-to-select after SECONDS (default: off, require Enter)"
    )
    parser.add_argument(
        "--scroll-debounce", type=float, default=0.15, metavar="SECONDS",
        help="Minimum time between scroll events (default: 0.15s)"
    )
    parser.add_argument(
        "--append-option", action="append", default=[], metavar="LABEL",
        help="Always append this option to every choice list (repeatable)"
    )
    parser.add_argument(
        "--append-silent-option", action="append", default=[], metavar="LABEL",
        help="Append option that is NOT read aloud during intro (repeatable). "
        "Format: 'title' or 'title::description'"
    )
    parser.add_argument(
        "--demo", action="store_true",
        help="Demo mode: show test choices immediately, no MCP server"
    )
    parser.add_argument(
        "--freeform-tts", choices=["api", "local"], default="local",
        help="TTS backend for freeform typing readback (default: local)"
    )
    parser.add_argument(
        "--freeform-tts-speed", type=float, default=1.6, metavar="SPEED",
        help="TTS speed multiplier for freeform readback (default: 1.6)"
    )
    parser.add_argument(
        "--freeform-tts-delimiters", default=" .,;:!?",
        help="Characters that trigger TTS readback while typing (default: ' .,;:!?')"
    )
    parser.add_argument(
        "--invert", action="store_true",
        help="Invert scroll direction (scroll-down → cursor-up, scroll-up → cursor-down)"
    )
    parser.add_argument(
        "--config-file", default=None, metavar="PATH",
        help="Path to config YAML file (default: ~/.config/io-mcp/config.yml)"
    )
    args = parser.parse_args()

    # Default append option: always offer to generate more options
    if not args.append_option:
        args.append_option = ["More options"]

    # Load config
    config = IoMcpConfig.load(args.config_file)
    print(f"  Config: {config.config_path}", flush=True)
    print(f"  TTS: model={config.tts_model_name}, voice={config.tts_voice}, speed={config.tts_speed}", flush=True)
    print(f"  STT: model={config.stt_model_name}, realtime={config.stt_realtime}", flush=True)

    tts = TTSEngine(local=args.local, config=config)

    # Separate TTS engine for freeform typing readback (can be different backend/speed)
    freeform_local = args.freeform_tts == "local"
    freeform_tts = TTSEngine(local=freeform_local, speed=args.freeform_tts_speed, config=config)

    # Create the textual app
    app = IoMcpApp(
        tts=tts,
        freeform_tts=freeform_tts,
        freeform_delimiters=args.freeform_tts_delimiters,
        dwell_time=args.dwell,
        scroll_debounce=args.scroll_debounce,
        invert_scroll=args.invert,
        demo=args.demo,
        config=config,
    )

    if args.demo:
        # Demo mode: loop test choices, no MCP server
        def _demo_loop():
            import time
            time.sleep(0.5)  # let textual mount
            # Create a demo session
            demo_session, _ = app.manager.get_or_create("demo")
            demo_session.name = "Demo"

            round_num = 0
            while True:
                round_num += 1
                choices = [
                    {"label": "Fix the bug", "summary": "There's a null pointer in the auth module on line 42"},
                    {"label": "Run the tests", "summary": "Execute the full test suite and report failures"},
                    {"label": "Show the diff", "summary": "Display what changed since the last commit"},
                    {"label": "Deploy to staging", "summary": "Push current branch to the staging environment"},
                ]
                # Append persistent options
                for opt in args.append_option:
                    if "::" in opt:
                        title, desc = opt.split("::", 1)
                    else:
                        title, desc = opt, ""
                    if not any(c["label"].lower() == title.lower() for c in choices):
                        choices.append({"label": title, "summary": desc})

                result = app.present_choices(
                    demo_session,
                    f"Demo round {round_num}. Pick any option to test scrolling and TTS.",
                    choices,
                )
                selected = result.get("selected", "")
                if selected == "quit":
                    break
                # Brief pause then loop
                time.sleep(0.3)

        demo_thread = threading.Thread(target=_demo_loop, daemon=True)
        demo_thread.start()
    else:
        # Kill any existing io-mcp instance so we can rebind the port
        _kill_existing_instance()

        # Write PID file so global hooks can detect io-mcp is running
        _write_pid_file()
        atexit.register(_remove_pid_file)

        # Acquire wake lock on Android to prevent sleep
        _acquire_wake_lock()
        atexit.register(_release_wake_lock)

        # Start MCP streamable-http server in background thread
        mcp_thread = threading.Thread(
            target=_run_mcp_server,
            args=(app, args.host, args.port, args.append_option, args.append_silent_option),
            daemon=True,
        )
        mcp_thread.start()

    # Run textual app in main thread (needs signal handlers)
    app.run()


if __name__ == "__main__":
    main()
