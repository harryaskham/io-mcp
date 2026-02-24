"""Thin MCP proxy server for io-mcp.

Serves the /mcp endpoint that agents connect to (port 8444).
Delegates all tool calls to the io-mcp backend via HTTP POST to /handle-mcp.
This separation allows restarting the backend without disconnecting agents.

Usage:
    io-mcp server                    # Start as daemon
    io-mcp server --foreground       # Run in foreground (for debugging)
    io-mcp server --io-mcp-address localhost:8446
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.request
import urllib.error
from typing import Any

from mcp.server.fastmcp import FastMCP, Context

log = logging.getLogger("io-mcp.proxy")

PID_FILE = "/tmp/io-mcp-server.pid"
DEFAULT_BACKEND = "http://localhost:8446"


def _forward_to_backend(
    backend_url: str,
    tool_name: str,
    args: dict[str, Any],
    session_id: str,
    max_retries: int = 30,
    initial_backoff: float = 0.5,
    max_backoff: float = 10.0,
) -> str:
    """Forward an MCP tool call to the io-mcp backend.

    Retries with exponential backoff if the backend is unavailable
    (e.g. during a restart). This is the key feature that lets us
    restart the backend without losing agent connections.

    Args:
        backend_url: Base URL of the backend (e.g. http://localhost:8446)
        tool_name: Name of the MCP tool to call
        args: Tool arguments
        session_id: MCP session ID for session routing
        max_retries: Maximum number of retries before giving up
        initial_backoff: Initial retry delay in seconds
        max_backoff: Maximum retry delay in seconds

    Returns:
        JSON string result from the tool
    """
    url = f"{backend_url}/handle-mcp"
    payload = json.dumps({
        "tool": tool_name,
        "args": args,
        "session_id": session_id,
    }).encode()

    backoff = initial_backoff
    last_error = ""

    for attempt in range(max_retries):
        try:
            req = urllib.request.Request(
                url,
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=300) as resp:
                return resp.read().decode()
        except urllib.error.HTTPError as e:
            # Backend returned an error — don't retry, return it
            try:
                body = e.read().decode()
                return body + _crash_log_hint()
            except Exception:
                return json.dumps({"error": f"Backend HTTP {e.code}"}) + _crash_log_hint()
        except (urllib.error.URLError, ConnectionRefusedError, OSError) as e:
            last_error = str(e)
            if attempt < max_retries - 1:
                log.debug(f"Backend unavailable (attempt {attempt + 1}), retrying in {backoff:.1f}s: {e}")
                time.sleep(backoff)
                backoff = min(backoff * 1.5, max_backoff)
            else:
                log.error(f"Backend unavailable after {max_retries} attempts: {e}")
        except Exception as e:
            return json.dumps({"error": f"Proxy error: {str(e)[:200]}"}) + _crash_log_hint()

    return json.dumps({
        "error": f"Backend unavailable after {max_retries} retries: {last_error}",
        "hint": "Is io-mcp running? Start it with: io-mcp",
    }) + _crash_log_hint()


def _crash_log_hint() -> str:
    """Read recent crash logs and return self-healing instructions.

    Appended to error responses so agents can diagnose and fix issues.
    """
    logs = []
    for log_file in ("/tmp/io-mcp-tui-error.log", "/tmp/io-mcp-tool-error.log"):
        try:
            with open(log_file, "r") as f:
                content = f.read()
            if content.strip():
                # Get last 2000 chars to avoid overwhelming the context
                tail = content[-2000:] if len(content) > 2000 else content
                logs.append(f"\n--- {log_file} ---\n{tail}")
        except FileNotFoundError:
            pass
        except Exception:
            pass

    if not logs:
        return ""

    return (
        "\n\n[IO-MCP CRASH DIAGNOSTICS]\n"
        "The io-mcp backend encountered an error. Recent crash logs:\n"
        + "\n".join(logs)
        + "\n\n[SELF-HEALING INSTRUCTIONS]\n"
        "1. If you can identify a code bug in io-mcp source, fix it and "
        "call pull_latest() or request_restart() to apply the fix.\n"
        "2. If the error is transient, retry your last operation.\n"
        "3. If the backend is completely down, tell the user via fallback TTS.\n"
        "4. The io-mcp source is at: src/io_mcp/ (tui/app.py, __main__.py, proxy.py, tts.py)\n"
    )


def _get_session_id(ctx: Context) -> str:
    """Extract session ID from MCP context."""
    session = ctx.session
    sid = getattr(session, "mcp_session_id", None)
    return str(sid) if sid else str(id(session))


def create_proxy_server(
    host: str = "0.0.0.0",
    port: int = 8444,
    backend_url: str = DEFAULT_BACKEND,
) -> FastMCP:
    """Create the MCP proxy server with all tool definitions.

    Each tool is a thin wrapper that forwards the call to the backend.
    The tool signatures and docstrings match the real tools exactly
    so that agents see the same API.
    """
    server = FastMCP("io-mcp", host=host, port=port)

    def _fwd(tool_name: str, args: dict, ctx: Context) -> str:
        sid = _get_session_id(ctx)
        return _forward_to_backend(backend_url, tool_name, args, sid)

    # ─── Tool definitions (thin proxies) ──────────────────────────

    @server.tool()
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
        return _fwd("present_choices", {"preamble": preamble, "choices": choices}, ctx)

    @server.tool()
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
        return _fwd("present_multi_select", {"preamble": preamble, "choices": choices}, ctx)

    @server.tool()
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
        return _fwd("speak", {"text": text}, ctx)

    @server.tool()
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
        return _fwd("speak_async", {"text": text}, ctx)

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
        return _fwd("speak_urgent", {"text": text}, ctx)

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
        return _fwd("set_speed", {"speed": speed}, ctx)

    @server.tool()
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
        return _fwd("set_voice", {"voice": voice}, ctx)

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
        return _fwd("set_tts_model", {"model": model}, ctx)

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
        return _fwd("set_stt_model", {"model": model}, ctx)

    @server.tool()
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
        return _fwd("set_emotion", {"emotion": emotion}, ctx)

    @server.tool()
    async def get_settings(ctx: Context) -> str:
        """Get the current io-mcp settings.

        Returns
        -------
        str
            JSON string with current TTS model, voice, speed, and STT model.
        """
        return _fwd("get_settings", {}, ctx)

    @server.tool()
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
        return _fwd("register_session", {
            "cwd": cwd, "hostname": hostname,
            "tmux_session": tmux_session, "tmux_pane": tmux_pane,
            "name": name, "voice": voice, "emotion": emotion,
            "metadata": metadata,
        }, ctx)

    @server.tool()
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
        return _fwd("rename_session", {"name": name}, ctx)

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
        return _fwd("reload_config", {}, ctx)

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
        return _fwd("pull_latest", {}, ctx)

    @server.tool()
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
        return _fwd("run_command", {"command": command}, ctx)

    @server.tool()
    async def request_restart(ctx: Context) -> str:
        """Request a restart of the io-mcp backend.

        Shows a confirmation dialog in the TUI. If approved, the backend
        restarts (TUI, TTS, session state reload) while this MCP proxy
        stays running — so your connection is preserved.

        Returns
        -------
        str
            JSON with status: "accepted", "rejected", or "error".
        """
        return _fwd("request_restart", {}, ctx)

    @server.tool()
    async def request_proxy_restart(ctx: Context) -> str:
        """Request a restart of the MCP proxy server.

        This will BREAK your MCP connection. After this returns success,
        your next MCP call will fail. You must reconnect by sending
        /mcp to your own Claude Code tmux pane:

            tmux send-keys -t %<your-pane> '/mcp' Enter
            sleep 3
            tmux send-keys -t %<your-pane> Enter  # select /mcp
            sleep 2
            tmux send-keys -t %<your-pane> Enter  # select io-mcp
            sleep 2
            tmux send-keys -t %<your-pane> Down Enter  # Reconnect
            sleep 4
            tmux send-keys -t %<your-pane> Escape Escape

        Returns
        -------
        str
            JSON with status and reconnect instructions.
        """
        # Ask the backend to confirm with the user
        result_str = _fwd("request_proxy_restart", {}, ctx)
        try:
            result = json.loads(result_str)
        except (json.JSONDecodeError, TypeError):
            return result_str

        if result.get("status") != "accepted":
            return result_str

        # Schedule proxy restart AFTER this response is sent
        import threading

        def _do_proxy_restart():
            import time as _t
            _t.sleep(1.0)  # Let the MCP response complete
            import os as _os
            import sys as _sys
            _os.execv(_sys.executable, [_sys.executable] + _sys.argv)

        threading.Thread(target=_do_proxy_restart, daemon=True).start()

        return json.dumps({
            "status": "accepted",
            "message": "Proxy will restart in ~1 second. Your MCP connection will break.",
            "reconnect_instructions": (
                "Your next MCP tool call will fail. To reconnect, send /mcp to your "
                "own Claude Code tmux pane using tmux send-keys, then navigate to "
                "the io-mcp server and select Reconnect. Use tmux-cli or tmux "
                "send-keys -t %<your-pane-id> '/mcp' Enter, then Enter, Enter, "
                "Down, Enter to navigate the menu, then Escape to close."
            ),
        })

    @server.tool()
    async def check_inbox(ctx: Context) -> str:
        """Check for queued user messages without waiting for another tool call.

        Returns any messages the user has queued via the 'm' key or
        message input. Use this to poll for user messages during long
        operations where you haven't made a tool call in a while.

        Returns
        -------
        str
            JSON with messages array and count.
        """
        return _fwd("check_inbox", {}, ctx)

    return server


def run_proxy_server(
    host: str = "0.0.0.0",
    port: int = 8444,
    backend_url: str = DEFAULT_BACKEND,
    foreground: bool = True,
) -> None:
    """Run the MCP proxy server.

    Always runs in foreground — daemonization is handled by the parent
    process via subprocess.Popen with start_new_session=True.
    """
    _write_pid(os.getpid())

    # Suppress noisy logs
    for name in ("mcp", "uvicorn", "uvicorn.access", "uvicorn.error", "httpx"):
        _log = logging.getLogger(name)
        _log.setLevel(logging.WARNING)
        _log.handlers = []

    server = create_proxy_server(host=host, port=port, backend_url=backend_url)

    log.info(f"MCP proxy server starting on {host}:{port}, backend={backend_url}")
    try:
        with open("/tmp/io-mcp-server.log", "a") as f:
            f.write(f"MCP proxy on {host}:{port} → backend {backend_url}\n")
    except Exception:
        pass

    server.run(transport="streamable-http")


def _write_pid(pid: int) -> None:
    """Write PID file."""
    try:
        with open(PID_FILE, "w") as f:
            f.write(str(pid))
    except Exception:
        pass


def _read_pid() -> int | None:
    """Read PID from file."""
    try:
        with open(PID_FILE, "r") as f:
            return int(f.read().strip())
    except (FileNotFoundError, ValueError):
        return None


def is_server_running() -> bool:
    """Check if the proxy server is already running."""
    pid = _read_pid()
    if pid is None:
        return False
    try:
        os.kill(pid, 0)  # Signal 0 = check if process exists
        return True
    except (ProcessLookupError, PermissionError):
        return False


def check_health(address: str) -> bool:
    """Check if the proxy server is healthy.

    Uses PID file check (the proxy doesn't expose a /health endpoint).
    """
    return is_server_running()
