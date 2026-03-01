"""
io-mcp — MCP server for agent I/O via scroll-wheel and TTS.

Two-process architecture:
  io-mcp server    Thin MCP proxy daemon on :8444 — agents connect here.
                   Survives backend restarts so agents stay connected.

  io-mcp           Main backend with TUI, TTS, session logic on :8446.
                   Exposes /handle-mcp for the proxy to call.
                   Also serves Android SSE API on :8445.
                   Auto-starts the proxy daemon if not already running.

Usage:
    uv run io-mcp                    # Start backend (auto-starts proxy)
    uv run io-mcp server             # Start proxy daemon only
    uv run io-mcp server --foreground  # Proxy in foreground (debug)
    uv run io-mcp --demo             # Demo mode, no MCP
    uv run io-mcp --reset-config     # Delete config.yml and regenerate with defaults
"""

from __future__ import annotations

import argparse
import atexit
import json
import logging
import os
import signal
import subprocess
import sys
import threading

from .config import IoMcpConfig
from .tui import IoMcpApp
from .tts import TTSEngine
from .logging import get_logger, log_context, TUI_ERROR_LOG, TOOL_ERROR_LOG

log = logging.getLogger("io_mcp")
_file_log = get_logger("io-mcp.main", TUI_ERROR_LOG)
_tool_log = get_logger("io-mcp.tools", TOOL_ERROR_LOG)

PID_FILE = "/tmp/io-mcp.pid"
PROXY_PID_FILE = "/tmp/io-mcp-server.pid"

DEFAULT_PROXY_PORT = 8444
DEFAULT_BACKEND_PORT = 8446
DEFAULT_API_PORT = 8445


def _write_pid_file() -> None:
    with open(PID_FILE, "w") as f:
        f.write(str(os.getpid()))


def _remove_pid_file() -> None:
    try:
        os.unlink(PID_FILE)
    except OSError:
        pass


def _kill_existing_backend() -> None:
    """Kill any previous io-mcp backend so we can rebind port 8446."""
    # Kill by PID file
    try:
        with open(PID_FILE, "r") as f:
            old_pid = int(f.read().strip())
        if old_pid != os.getpid():
            os.kill(old_pid, signal.SIGTERM)
            import time
            time.sleep(0.3)
            try:
                os.kill(old_pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
    except (FileNotFoundError, ValueError, ProcessLookupError, PermissionError):
        pass

    # Also kill anything holding the backend port
    _kill_port_holder(DEFAULT_BACKEND_PORT)


def _kill_port_holder(port: int) -> None:
    """Kill whatever process is holding a TCP port.

    Tries multiple methods since not all are available on every platform:
    1. fuser (Linux)
    2. lsof (macOS/Linux)
    3. /proc/net/tcp scan (Linux/Android, no root needed)
    """
    import time

    # Method 1: fuser
    try:
        result = subprocess.run(["fuser", f"{port}/tcp"],
                                timeout=3, capture_output=True, text=True)
        for pid_str in result.stdout.strip().split():
            try:
                pid = int(pid_str.strip())
                if pid != os.getpid():
                    os.kill(pid, signal.SIGKILL)
            except (ValueError, ProcessLookupError, PermissionError):
                pass
        time.sleep(0.3)
        return
    except (FileNotFoundError, Exception):
        pass

    # Method 2: lsof
    try:
        result = subprocess.run(["lsof", "-ti", f":{port}"],
                                timeout=3, capture_output=True, text=True)
        for pid_str in result.stdout.strip().split("\n"):
            try:
                pid = int(pid_str.strip())
                if pid != os.getpid():
                    os.kill(pid, signal.SIGKILL)
            except (ValueError, ProcessLookupError, PermissionError):
                pass
        time.sleep(0.3)
        return
    except (FileNotFoundError, Exception):
        pass

    # Method 3: scan /proc/net/tcp (works on Android/Linux without root)
    try:
        hex_port = f"{port:04X}"
        with open("/proc/net/tcp", "r") as f:
            for line in f:
                parts = line.strip().split()
                if len(parts) < 10:
                    continue
                local = parts[1]
                if local.endswith(f":{hex_port}"):
                    try:
                        inode = int(parts[9])
                    except (ValueError, IndexError):
                        continue
                    # Find PID owning this inode via /proc/*/fd
                    for pid_dir in os.listdir("/proc"):
                        if not pid_dir.isdigit():
                            continue
                        try:
                            fd_dir = f"/proc/{pid_dir}/fd"
                            for fd in os.listdir(fd_dir):
                                link = os.readlink(f"{fd_dir}/{fd}")
                                if f"socket:[{inode}]" in link:
                                    pid = int(pid_dir)
                                    if pid != os.getpid():
                                        os.kill(pid, signal.SIGKILL)
                                        time.sleep(0.3)
                                    return
                        except (PermissionError, FileNotFoundError, OSError):
                            continue
    except (FileNotFoundError, PermissionError):
        pass


def _force_kill_all() -> None:
    """Force kill ALL io-mcp processes and free ports 8444/8446.

    Used by --restart to ensure a clean slate.
    """
    import time

    # Kill by PID files
    for pidfile in [PID_FILE, PROXY_PID_FILE]:
        try:
            with open(pidfile, "r") as f:
                pid = int(f.read().strip())
            os.kill(pid, signal.SIGTERM)
            time.sleep(0.2)
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        except (FileNotFoundError, ValueError, ProcessLookupError, PermissionError):
            pass

    # Kill by process name pattern
    try:
        subprocess.run(["pkill", "-f", "io_mcp.server"], timeout=3,
                       capture_output=True)
        subprocess.run(["pkill", "-f", "io_mcp server"], timeout=3,
                       capture_output=True)
    except Exception:
        pass

    # Kill anything on our ports
    for port in [DEFAULT_PROXY_PORT, DEFAULT_BACKEND_PORT]:
        _kill_port_holder(port)

    # Clean up PID files
    for pidfile in [PID_FILE, PROXY_PID_FILE]:
        try:
            os.unlink(pidfile)
        except OSError:
            pass

    time.sleep(0.5)
    print("  Restart: killed all io-mcp processes", flush=True)


def _acquire_wake_lock() -> None:
    import shutil
    termux_exec = shutil.which("termux-exec")
    if not termux_exec:
        return
    try:
        subprocess.Popen(
            [termux_exec, "termux-wake-lock"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        print("  Wake lock: acquired", flush=True)
    except Exception:
        pass


def _release_wake_lock() -> None:
    import shutil
    termux_exec = shutil.which("termux-exec")
    if not termux_exec:
        return
    try:
        subprocess.run([termux_exec, "termux-wake-unlock"], timeout=3, capture_output=True)
    except Exception:
        pass


def _is_local_address(addr: str) -> bool:
    host = addr.split(":")[0] if ":" in addr else addr
    return host in ("localhost", "127.0.0.1", "0.0.0.0", "::1", "")


def _is_proxy_alive() -> bool:
    """Check if the proxy daemon process is alive via PID file."""
    try:
        with open(PROXY_PID_FILE, "r") as f:
            pid = int(f.read().strip())
        os.kill(pid, 0)  # signal 0 = check if alive
        return True
    except (FileNotFoundError, ValueError, ProcessLookupError, PermissionError):
        return False


def _ensure_proxy_running(proxy_address: str, backend_port: int, dev: bool = False) -> None:
    """Start the MCP proxy daemon if not already running."""
    if _is_proxy_alive():
        print(f"  Proxy: already running (PID in {PROXY_PID_FILE})", flush=True)
        return

    if not _is_local_address(proxy_address):
        print(f"  ERROR: Proxy at {proxy_address} is not running and is remote", flush=True)
        print(f"  Start it manually: io-mcp server --io-mcp-address <this-machine>:{backend_port}", flush=True)
        sys.exit(1)

    parts = proxy_address.split(":")
    proxy_host = "0.0.0.0"  # Always bind all interfaces so remote agents can connect
    proxy_port = int(parts[1]) if len(parts) > 1 else DEFAULT_PROXY_PORT

    print(f"  Proxy: starting daemon on {proxy_host}:{proxy_port}...", flush=True)

    try:
        # Kill any stale process holding the port
        try:
            with open(PROXY_PID_FILE, "r") as f:
                old_pid = int(f.read().strip())
            os.kill(old_pid, signal.SIGTERM)
            import time
            time.sleep(0.5)
            try:
                os.kill(old_pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        except (FileNotFoundError, ValueError, ProcessLookupError, PermissionError):
            pass

        # Start proxy as a detached subprocess
        # --dev forces uv run; otherwise try installed 'io-mcp' script
        import shutil
        io_mcp_bin = None if dev else shutil.which("io-mcp")
        if io_mcp_bin:
            cmd = [io_mcp_bin, "server",
                   "--host", proxy_host,
                   "--port", str(proxy_port),
                   "--io-mcp-address", f"localhost:{backend_port}",
                   "--foreground"]
            cwd = None
        else:
            # Fallback: use python -m (for dev mode / uv run)
            project_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            cmd = [sys.executable, "-m", "io_mcp", "server",
                   "--host", proxy_host,
                   "--port", str(proxy_port),
                   "--io-mcp-address", f"localhost:{backend_port}",
                   "--foreground"]
            cwd = project_dir

        log_file = open("/tmp/io-mcp-server.log", "w")
        proc = subprocess.Popen(
            cmd,
            stdout=log_file,
            stderr=log_file,
            cwd=cwd,
            start_new_session=True,  # Detach from parent process group
        )
        # Close the file descriptor in the parent — child inherited it
        log_file.close()
        # Write PID file ourselves since the child runs in foreground mode
        with open(PROXY_PID_FILE, "w") as f:
            f.write(str(proc.pid))

        import time
        time.sleep(2.0)  # Give uvicorn time to bind

        if proc.poll() is None:
            print(f"  Proxy: started (PID {proc.pid})", flush=True)
        else:
            print(f"  Proxy: process exited immediately — check /tmp/io-mcp-server.log", flush=True)
    except Exception as e:
        print(f"  Proxy: failed to start — {e}", flush=True)

def _detect_hostname() -> str:
    """Detect the local hostname, preferring Tailscale DNS name.

    Checks tailscale status --json for the self node's DNS name first
    (e.g. 'harrys-macbook-pro' from 'harrys-macbook-pro.domain.ts.net.'),
    then falls back to HostName, then socket.gethostname().

    Only caches the result if it's a useful value (not 'localhost' or empty).
    This way, if Tailscale isn't ready at startup, subsequent calls can retry.
    """
    cached = getattr(_detect_hostname, '_cached', None)
    if cached is not None:
        return cached

    import socket
    hostname = ""

    # Try Tailscale first
    try:
        import subprocess
        result = subprocess.run(
            ["tailscale", "status", "--json"],
            capture_output=True, text=True, timeout=3,
        )
        if result.returncode == 0:
            import json as _json
            ts = _json.loads(result.stdout)
            self_node = ts.get("Self", {})
            # Prefer DNSName (clean, SSH-able) over HostName (may have spaces)
            dns_name = self_node.get("DNSName", "")
            if dns_name:
                # Strip trailing dot and ts.net domain
                # e.g. "harrys-macbook-pro.miku-owl.ts.net." → "harrys-macbook-pro"
                hostname = dns_name.rstrip(".").split(".")[0]
            if not hostname:
                ts_hostname = self_node.get("HostName", "")
                if ts_hostname:
                    hostname = ts_hostname
    except Exception:
        _file_log.debug("Tailscale hostname detection failed", exc_info=True)

    # Fallback to socket hostname
    if not hostname:
        hostname = socket.gethostname()
        # Strip .local suffix
        if hostname.endswith(".local"):
            hostname = hostname[:-6]

    # Only cache useful values — retry Tailscale on next call if we got a bad result
    if hostname and hostname != "localhost":
        _detect_hostname._cached = hostname

    return hostname


# ─── Backend tool dispatcher ──────────────────────────────────────────

def _create_tool_dispatcher(app_ref: list, append_options: list[str],
                            append_silent_options: list[str]):
    """Create a function that dispatches tool calls to the app.

    Args:
        app_ref: Mutable list containing [app] so the dispatch function
                 always uses the current app (survives TUI restarts).
        append_options: Extra options to append to present_choices.
        append_silent_options: Silent extra options.

    Returns a callable(tool_name, args, session_id) -> str
    """
    # Adapt IoMcpApp to the Frontend protocol via mutable reference
    class _AppFrontend:
        @property
        def _app(self):
            return app_ref[0]
        @property
        def manager(self):
            return self._app.manager
        @property
        def config(self):
            return self._app._config
        @property
        def tts(self):
            return self._app._tts

        def present_choices(self, session, preamble, choices):
            return self._app.present_choices(session, preamble, choices)
        def present_multi_select(self, session, preamble, choices):
            return self._app.present_multi_select(session, preamble, choices)
        def session_speak(self, session, text, block=True, priority=0, emotion=""):
            return self._app.session_speak(session, text, block, priority, emotion)
        def session_speak_async(self, session, text):
            return self._app.session_speak(session, text, block=False)
        def on_session_created(self, session):
            return self._app.on_session_created(session)
        def update_tab_bar(self):
            self._app.call_from_thread(self._app._update_tab_bar)
        def update_footer_status(self):
            self._app.call_from_thread(self._app._update_footer_status)
        def hot_reload(self):
            self._app.call_from_thread(self._app.action_hot_reload)
        def notify_inbox_update(self, session):
            """Notify the TUI that a session's inbox has new items."""
            self._app.notify_inbox_update(session)

    frontend = _AppFrontend()

    import time as _time

    # Build set of TUI extra labels for deduplication.
    # Options matching TUI extras shouldn't be appended as numbered choices
    # since the TUI already displays them in its collapsed extras section.
    from .tui.widgets import PRIMARY_EXTRAS, SECONDARY_EXTRAS, MORE_OPTIONS_ITEM
    _tui_extra_labels: set[str] = set()
    for e in PRIMARY_EXTRAS + SECONDARY_EXTRAS + [MORE_OPTIONS_ITEM]:
        _tui_extra_labels.add(e["label"].lower().rstrip(" ›"))

    # Build config-based extra options, filtering out any that duplicate TUI extras
    _config_extras: list[dict] = []
    if frontend.config:
        for opt in frontend.config.extra_options:
            label = opt.get("title", "")
            if label.lower().rstrip(" ›") in _tui_extra_labels:
                continue  # skip — already handled by TUI extras
            _config_extras.append({
                "label": label,
                "summary": opt.get("description", ""),
                "_silent": opt.get("silent", False),
            })

    def _drain_messages(session) -> list[str]:
        msgs = getattr(session, 'pending_messages', None)
        if msgs is None:
            return []
        drained = list(msgs)
        if drained:
            # Move to flushed_messages for chat view tracking
            from io_mcp.session import FlushedMessage
            now = _time.time()
            flushed = getattr(session, 'flushed_messages', None)
            if flushed is not None:
                for m in drained:
                    flushed.append(FlushedMessage(
                        text=m, queued_at=now, flushed_at=now,
                    ))
                cap = getattr(session, '_flushed_messages_max', 50)
                overflow = len(flushed) - cap
                if overflow > 0:
                    del flushed[:overflow]
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

    def _get_session(session_id: str):
        session, created = frontend.manager.get_or_create(session_id)
        if created:
            try:
                frontend.on_session_created(session)
            except Exception:
                _file_log.debug("on_session_created callback failed", exc_info=True)
        session.last_tool_call = _time.time()
        session.heartbeat_spoken = False
        session.ambient_count = 0
        return session

    def _registration_reminder(session) -> str:
        if not session.registered:
            return ("\n\n[REMINDER: Call register_session() first with your cwd, "
                    "hostname, tmux_session, and tmux_pane so io-mcp can manage "
                    "your session properly.]")
        return ""

    def _speech_reminder(session) -> str:
        """Remind the agent to narrate if it's been too long since last speech.

        Returns a reminder string if >60s since last speak call,
        empty string otherwise. Only fires for non-speak tool calls.
        """
        if not session.speech_log:
            return ""
        last_speech = session.speech_log[-1].timestamp
        elapsed = _time.time() - last_speech
        if elapsed > 60:
            return ("\n\n[REMINDER: It's been over a minute since you last used "
                    "speak_async(). The user is listening through earphones and "
                    "can't see the screen — narrate what you're doing!]")
        return ""

    def _touch_speech_timestamp(session):
        """Update the speech timestamp file for the PreToolUse nudge hook.

        The nudge-speak.sh hook checks this file to decide whether to remind
        the agent to narrate. We touch it on speak/speak_async/speak_urgent
        and present_choices (since preamble is spoken aloud).
        """
        try:
            # Use tmux_pane (sanitized) as identifier, matching the hook script
            pane = getattr(session, 'tmux_pane', '') or ''
            agent_id = pane.replace('%', '') if pane else session.session_id
            ts_file = f"/tmp/io-mcp-last-speech-{agent_id}"
            with open(ts_file, 'w') as f:
                f.write(str(int(_time.time())))
        except Exception:
            pass  # Best-effort — don't break tool calls

    # ─── Tool implementations ─────────────────────────────────

    def _tool_present_choices(args, session_id):
        session = _get_session(session_id)
        session.last_tool_name = "present_choices"
        _touch_speech_timestamp(session)  # preamble is spoken aloud
        preamble = args.get("preamble", "")
        choices = args.get("choices", [])
        timeout = args.get("timeout", None)  # Optional timeout in seconds
        if not choices:
            return json.dumps({"selected": "error", "summary": "No choices provided"})

        all_choices = list(choices)
        for opt in append_options:
            if "::" in opt:
                title, desc = opt.split("::", 1)
            else:
                title, desc = opt, ""
            # Skip options that duplicate TUI extras (e.g. "More options")
            if title.lower().rstrip(" ›") in _tui_extra_labels:
                continue
            if not any(c.get("label", "").lower() == title.lower() for c in all_choices):
                all_choices.append({"label": title, "summary": desc})
        for opt in append_silent_options:
            if "::" in opt:
                title, desc = opt.split("::", 1)
            else:
                title, desc = opt, ""
            # Skip options that duplicate TUI extras
            if title.lower().rstrip(" ›") in _tui_extra_labels:
                continue
            if not any(c.get("label", "").lower() == title.lower() for c in all_choices):
                all_choices.append({"label": title, "summary": desc, "_silent": True})
        for opt in _config_extras:
            if not any(c.get("label", "").lower() == opt["label"].lower() for c in all_choices):
                all_choices.append(dict(opt))

        restart_retries = 0
        max_restart_retries = 3  # Don't retry forever — proxy/agent may be gone

        while True:
            session.last_preamble = preamble
            session.last_choices = list(all_choices)

            if timeout is not None and timeout > 0:
                # Non-blocking: present choices and return after timeout
                # Choices stay visible — user can still select later
                import threading as _threading
                result_box = [None]
                def _present():
                    result_box[0] = frontend.present_choices(session, preamble, all_choices)
                t = _threading.Thread(target=_present, daemon=True)
                t.start()
                t.join(timeout=float(timeout))
                if result_box[0] is None:
                    # Timed out — resolve the active inbox item cleanly
                    _file_log.info("_tool_present_choices: timeout fired", extra={"context": {
                        "timeout": timeout,
                        "session": session.name,
                        "n_choices": len(all_choices),
                    }})
                    _active = getattr(session, '_active_inbox_item', None)
                    if _active and not _active.done:
                        _active.result = {"selected": "_timeout", "summary": "timed out"}
                        _active.done = True
                        _active.event.set()
                        session.drain_kick.set()
                    # Tell the TUI to show idle state
                    try:
                        frontend._app._safe_call(
                            lambda: frontend._app._show_waiting("(timed out)"))
                    except Exception:
                        pass
                    return _attach_messages(
                        json.dumps({"selected": "_timeout", "summary": f"No selection within {timeout}s — choices still visible"}),
                        session)
                result = result_box[0]
            else:
                _file_log.info("_tool_present_choices: blocking present", extra={"context": {
                    "session": session.name,
                    "preamble": preamble[:80],
                    "n_choices": len(all_choices),
                }})
                result = frontend.present_choices(session, preamble, all_choices)

            if result.get("selected") == "_cancelled":
                # MCP client cancelled the tool call — return immediately
                return json.dumps({"selected": "error", "summary": "Cancelled by client"})
            if result.get("selected") == "_dismissed":
                # User dismissed this choice without responding — return immediately
                return json.dumps({"selected": "_dismissed", "summary": "Dismissed by user"})
            if result.get("selected") == "_undo":
                restart_retries = 0
                continue
            if result.get("selected") == "_restart":
                restart_retries += 1
                if restart_retries > max_restart_retries:
                    # Too many restarts — the agent/proxy likely disconnected
                    return json.dumps({"selected": "error", "summary": "Aborted after too many TUI restarts"})
                # TUI is restarting — wait for the new app to be ready,
                # then re-create session and re-present choices
                _time.sleep(3.0)
                session = _get_session(session_id)
                session.last_tool_name = "present_choices"
                continue
            if result.get("selected") == "error" and "App is not running" in result.get("summary", ""):
                restart_retries += 1
                if restart_retries > max_restart_retries:
                    return json.dumps({"selected": "error", "summary": "Aborted — TUI not running"})
                # TUI crashed mid-presentation — treat as restart
                _time.sleep(3.0)
                session = _get_session(session_id)
                session.last_tool_name = "present_choices"
                continue
            break

        return _attach_messages(json.dumps(result), session) + _registration_reminder(session)

    def _tool_present_multi_select(args, session_id):
        session = _get_session(session_id)
        session.last_tool_name = "present_multi_select"
        preamble = args.get("preamble", "")
        choices = args.get("choices", [])
        if not choices:
            return json.dumps({"selected": []})
        restart_retries = 0
        while True:
            result = frontend.present_multi_select(session, preamble, list(choices))
            # Check for restart signal
            if isinstance(result, dict) and result.get("selected") == "_restart":
                restart_retries += 1
                if restart_retries > 3:
                    return json.dumps({"selected": [], "error": "Aborted after TUI restarts"})
                _time.sleep(3.0)
                session = _get_session(session_id)
                session.last_tool_name = "present_multi_select"
                continue
            break
        return _attach_messages(json.dumps({"selected": result}), session)

    def _tool_speak(args, session_id):
        session = _get_session(session_id)
        session.last_tool_name = "speak"
        _touch_speech_timestamp(session)
        text = args.get("text", "")
        # Enqueue as inbox item — agent blocks until TTS finishes
        item = session.enqueue_speech(text, blocking=True, priority=0)
        frontend.notify_inbox_update(session)
        item.event.wait(timeout=120)  # Don't block forever
        preview = text[:100] + ("..." if len(text) > 100 else "")
        return _attach_messages(f"Spoke: {preview}", session)

    def _tool_speak_async(args, session_id):
        session = _get_session(session_id)
        session.last_tool_name = "speak_async"
        _touch_speech_timestamp(session)
        text = args.get("text", "")
        # Enqueue as inbox item — agent returns immediately
        item = session.enqueue_speech(text, blocking=False, priority=0)
        frontend.notify_inbox_update(session)
        preview = text[:100] + ("..." if len(text) > 100 else "")
        return _attach_messages(f"Spoke: {preview}", session)

    def _tool_speak_urgent(args, session_id):
        session = _get_session(session_id)
        session.last_tool_name = "speak_urgent"
        _touch_speech_timestamp(session)
        text = args.get("text", "")
        # Enqueue at front of inbox with priority — agent blocks
        item = session.enqueue_speech(text, blocking=True, priority=1)
        frontend.notify_inbox_update(session)
        item.event.wait(timeout=120)  # Don't block forever
        preview = text[:100] + ("..." if len(text) > 100 else "")
        return _attach_messages(f"Urgently spoke: {preview}", session)

    def _tool_set_speed(args, session_id):
        if frontend.config:
            frontend.config.set_tts_speed(args.get("speed", 1.0))
            frontend.config.save()
            frontend.tts.clear_cache()
        return f"Speed set to {args.get('speed', 1.0)}"

    def _tool_set_voice(args, session_id):
        voice = args.get("voice", "")
        if frontend.config:
            valid = frontend.config.voice_preset_names
            if valid and voice not in valid:
                return json.dumps({
                    "error": f"Unsupported voice preset: {voice}",
                    "valid_voices": valid,
                })
            frontend.config.set_tts_voice(voice)
            frontend.config.save()
            frontend.tts.clear_cache()
        return f"Voice set to {voice}"

    def _tool_set_tts_model(args, session_id):
        model = args.get("model", "")
        if frontend.config:
            valid = frontend.config.tts_model_names
            if valid and model not in valid:
                return json.dumps({
                    "error": f"Unsupported TTS model: {model}",
                    "valid_models": valid,
                })
            frontend.config.set_tts_model(model)
            frontend.config.save()
            frontend.tts.clear_cache()
            return f"TTS model set to {model}, voice is now {frontend.config.tts_voice_preset}"
        return f"TTS model set to {model}"

    def _tool_set_stt_model(args, session_id):
        model = args.get("model", "")
        if frontend.config:
            valid = list(frontend.config.models.get("stt", {}).keys())
            if valid and model not in valid:
                return json.dumps({
                    "error": f"Unsupported STT model: {model}",
                    "valid_models": valid,
                })
            frontend.config.set_stt_model(model)
            frontend.config.save()
        return f"STT model set to {model}"

    def _tool_set_emotion(args, session_id):
        style = args.get("emotion", "")
        if frontend.config:
            valid = frontend.config.tts_style_options
            if valid and style not in valid:
                return json.dumps({
                    "error": f"Unsupported emotion/style: {style}",
                    "valid_styles": valid,
                })
            frontend.config.set_tts_style(style)
            frontend.config.save()
            frontend.tts.clear_cache()
        return f"Style set to: {style}"

    def _tool_get_settings(args, session_id):
        if frontend.config:
            result = {
                "tts_voice": frontend.config.tts_voice_preset,
                "tts_model": frontend.config.tts_model_name,
                "tts_speed": frontend.config.tts_speed,
                "tts_style": frontend.config.tts_style,
                "voices": frontend.config.voice_preset_names,
                "styles": frontend.config.tts_style_options,
                "stt_model": frontend.config.stt_model_name,
                "stt_models": frontend.config.stt_model_names,
            }
            if frontend.tts:
                result["api_health"] = frontend.tts.api_health
            return json.dumps(result)
        return json.dumps({"error": "No config available"})

    def _tool_register_session(args, session_id):
        session = _get_session(session_id)
        session.last_tool_name = "register_session"
        session.registered = True
        session.registered_at = _time.time()
        for field in ("cwd", "hostname", "tmux_session", "tmux_pane", "username"):
            val = args.get(field, "")
            if val:
                setattr(session, field, val)

        # Auto-detect hostname if agent didn't provide a useful one
        agent_hostname = args.get("hostname", "")
        needs_detect = (
            not agent_hostname
            or agent_hostname == "localhost"
            or agent_hostname.endswith(".local")
        )
        if needs_detect:
            detected = _detect_hostname()
            if detected:
                session.hostname = detected

        # Auto-detect username if not provided
        if not args.get("username"):
            import getpass
            try:
                session.username = getpass.getuser()
            except Exception:
                pass

        if args.get("name"):
            session.name = args["name"]
        if args.get("voice"):
            session.voice_override = args["voice"]
        if args.get("emotion"):
            session.emotion_override = args["emotion"]
        if args.get("metadata"):
            session.agent_metadata.update(args["metadata"])

        import socket
        local_hostname = _detect_hostname() or socket.gethostname()
        hostname = session.hostname or ""
        is_local = (hostname == local_hostname) if hostname else True

        try:
            frontend.update_tab_bar()
        except Exception:
            pass

        return json.dumps({
            "status": "registered",
            "session_id": session.session_id,
            "name": session.name,
            "is_local": is_local,
            "io_mcp_hostname": local_hostname,
            "features": list(TOOLS.keys()),
        })

    def _tool_rename_session(args, session_id):
        session = _get_session(session_id)
        session.name = args.get("name", "")
        try:
            frontend.update_tab_bar()
        except Exception:
            pass
        return f"Session renamed to: {session.name}"

    def _tool_reload_config(args, session_id):
        if frontend.config:
            frontend.config.reload()
            frontend.tts.clear_cache()
            frontend.tts.reset_failure_counters()
            return json.dumps({
                "status": "reloaded",
                "tts_voice": frontend.config.tts_voice_preset,
                "tts_model": frontend.config.tts_model_name,
                "tts_speed": frontend.config.tts_speed,
                "tts_style": frontend.config.tts_style,
                "stt_model": frontend.config.stt_model_name,
            })
        return json.dumps({"status": "no config to reload"})

    def _tool_pull_latest(args, session_id):
        import subprocess as sp
        project_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        try:
            result = sp.run(["git", "pull", "--rebase", "origin", "main"],
                          cwd=project_dir, capture_output=True, text=True, timeout=30)
            output = result.stdout.strip()
            if result.returncode != 0:
                return json.dumps({"status": "error", "output": output, "error": result.stderr.strip()})
            try:
                frontend.hot_reload()
                return json.dumps({"status": "pulled_and_refreshed", "output": output,
                                   "note": "Config refreshed. Restart TUI for code changes."})
            except Exception:
                return json.dumps({"status": "pulled", "output": output,
                                   "note": "Restart TUI for code changes."})
        except subprocess.TimeoutExpired:
            return json.dumps({"status": "error", "error": "Git pull timed out"})
        except Exception as e:
            return json.dumps({"status": "error", "error": str(e)})

    def _tool_run_command(args, session_id):
        import subprocess as sp
        session = _get_session(session_id)
        command = args.get("command", "")
        result = frontend.present_choices(session, f"Agent wants to run: {command}",
            [{"label": "Approve", "summary": f"Run: {command}"},
             {"label": "Deny", "summary": "Reject this command"}])
        if result.get("selected", "").lower() != "approve":
            return json.dumps({"status": "denied", "command": command})
        try:
            proc = sp.run(command, shell=True, capture_output=True, text=True, timeout=60)
            return json.dumps({"status": "completed", "command": command,
                             "returncode": proc.returncode,
                             "stdout": proc.stdout[:5000], "stderr": proc.stderr[:2000]})
        except sp.TimeoutExpired:
            return json.dumps({"status": "timeout", "command": command, "error": "Timed out after 60s"})
        except Exception as e:
            return json.dumps({"status": "error", "command": command, "error": str(e)})

    def _tool_request_restart(args, session_id):
        session = _get_session(session_id)
        session.last_tool_name = "request_restart"

        # Skip confirmation if config allows it
        auto_approve = (frontend.config and frontend.config.always_allow_restart_tui)
        if not auto_approve:
            result = frontend.present_choices(session,
                "Agent requests TUI restart. Sessions reset, MCP proxy stays alive.",
                [{"label": "Approve restart", "summary": "Restart io-mcp TUI now"},
                 {"label": "Deny", "summary": "Keep running"}])
            if result.get("selected", "").lower() != "approve restart":
                return _attach_messages(
                    json.dumps({"status": "rejected", "message": "User denied restart"}),
                    session)

        # Use the TUI restart loop (not os.execv which kills everything)
        def _do_restart():
            import time as _t
            _t.sleep(0.5)
            frontend.tts.speak_async("Restarting TUI...")
            _t.sleep(1.0)
            # Unblock all pending selections
            for sess in frontend.manager.all_sessions():
                if sess.active:
                    frontend.present_choices  # access to verify app is alive
                    app = frontend._app
                    app._restart_requested = True
                    # Resolve all active sessions
                    sess_result = {"selected": "_restart", "summary": "TUI restarting"}
                    _item = getattr(sess, '_active_inbox_item', None)
                    if _item and not _item.done:
                        _item.result = sess_result
                        _item.done = True
                        _item.event.set()
                    sess.selection = sess_result
                    sess.selection_event.set()
            # Trigger TUI exit with restart code
            try:
                app = frontend._app
                app._restart_requested = True
                app.call_from_thread(lambda: app.exit(return_code=42))
            except Exception:
                _file_log.debug("Failed to trigger TUI restart", exc_info=True)

        threading.Thread(target=_do_restart, daemon=True).start()
        return _attach_messages(
            json.dumps({"status": "accepted", "message": "TUI will restart in ~1.5 seconds. Proxy stays alive."}),
            session)

    def _tool_request_proxy_restart(args, session_id):
        session = _get_session(session_id)
        session.last_tool_name = "request_proxy_restart"
        result = frontend.present_choices(session,
            "Agent requests PROXY restart. This will break ALL agent MCP connections. They must reconnect.",
            [{"label": "Approve proxy restart", "summary": "Restart the MCP proxy — all agents disconnect"},
             {"label": "Deny", "summary": "Keep proxy running"}])
        if result.get("selected", "").lower() != "approve proxy restart":
            return _attach_messages(
                json.dumps({"status": "rejected", "message": "User denied proxy restart"}),
                session)

        # Actually restart the proxy in a background thread
        def _do_proxy_restart():
            import time as _t
            _t.sleep(0.3)
            frontend.tts.speak_async("Restarting MCP proxy...")
            # Determine dev mode from sys.argv
            dev_mode = "--dev" in sys.argv
            proxy_addr = f"localhost:{DEFAULT_PROXY_PORT}"
            success = _restart_proxy(proxy_addr, DEFAULT_BACKEND_PORT, dev=dev_mode)
            if success:
                frontend.tts.speak_async("Proxy restarted. Agents need to reconnect.")
            else:
                frontend.tts.speak_async("Proxy restart failed. Check logs.")

        threading.Thread(target=_do_proxy_restart, daemon=True).start()
        return _attach_messages(
            json.dumps({"status": "accepted", "message": "Proxy restarting. Agents must reconnect."}),
            session)

    def _tool_request_close(args, session_id):
        """Request closing this agent's session with user confirmation."""
        session = _get_session(session_id)
        session.last_tool_name = "request_close"
        reason = args.get("reason", "Work complete")

        # Present confirmation to user
        result = frontend.present_choices(session,
            f"Agent wants to close session: {reason}",
            [{"label": "Accept", "summary": f"Close this session ({session.name})"},
             {"label": "Decline", "summary": "Keep the session open"}])

        if result.get("selected", "").lower() == "accept":
            # Close the session
            name = session.name
            try:
                app = frontend._app
                app.call_from_thread(lambda: app.on_session_removed(session_id))
            except Exception:
                # Fallback: remove directly from manager
                _file_log.debug("call_from_thread failed for session removal, using fallback", exc_info=True)
                frontend.manager.remove(session_id)
                try:
                    frontend.update_tab_bar()
                except Exception:
                    _file_log.debug("Fallback update_tab_bar also failed", exc_info=True)
            return json.dumps({"status": "closed", "message": f"Session '{name}' closed."})

        # User declined — ask for a reason
        decline_result = frontend.present_choices(session,
            "Why should the agent continue?",
            [{"label": "Keep working", "summary": "Continue with more tasks"},
             {"label": "Review changes", "summary": "Review what was done before closing"},
             {"label": "Something else", "summary": "I have other instructions"}])

        decline_reason = decline_result.get("selected", "Keep working")
        return _attach_messages(json.dumps({
            "status": "declined",
            "reason": decline_reason,
            "message": f"User wants the agent to continue: {decline_reason}",
        }), session)

    def _tool_check_inbox(args, session_id):
        """Check for queued user messages without waiting for another tool call."""
        session = _get_session(session_id)
        session.last_tool_name = "check_inbox"
        session.tool_call_count += 1
        messages = _drain_messages(session)
        if messages:
            return json.dumps({"messages": messages, "count": len(messages)})
        return json.dumps({"messages": [], "count": 0})

    def _tool_report_status(args, session_id):
        """Report a lightweight status update to the activity feed."""
        session = _get_session(session_id)
        session.last_tool_name = "report_status"
        session.tool_call_count += 1
        status = args.get("status", "")
        if not status:
            return json.dumps({"error": "No status provided"})
        session.log_activity("report_status", status[:120], kind="status")
        # Update TUI to show the new activity
        try:
            frontend.update_tab_bar()
            frontend.update_footer_status()
        except Exception:
            pass
        return _attach_messages(json.dumps({"status": "logged", "text": status[:120]}), session)

    def _tool_get_logs(args, session_id):
        """Return recent io-mcp logs: TUI errors, proxy output, and backend stderr."""
        session = _get_session(session_id)
        session.last_tool_name = "get_logs"
        session.tool_call_count += 1

        lines = int(args.get("lines", 50))
        logs = {}

        # TUI error log
        from .logging import read_log_tail, TUI_ERROR_LOG as _tui_log, PROXY_LOG as _proxy_log
        logs["tui_errors"] = read_log_tail(_tui_log, lines)

        # Proxy log (if exists)
        logs["proxy"] = read_log_tail(_proxy_log, lines)

        # Session speech log (recent entries for this session)
        speech = []
        for entry in session.speech_log[-lines:]:
            speech.append(f"[{entry.timestamp:.0f}] {entry.text[:200]}")
        logs["speech_log"] = speech

        # TTS health status
        try:
            logs["tts_health"] = frontend.tts.tts_health
        except Exception:
            logs["tts_health"] = {"status": "unknown"}

        return _attach_messages(json.dumps(logs), session)

    def _tool_get_sessions(args, session_id):
        """List all active agent sessions with their status and metadata."""
        session = _get_session(session_id)
        session.last_tool_name = "get_sessions"
        session.tool_call_count += 1

        sessions = []
        for sid in frontend.manager.session_order:
            s = frontend.manager.sessions.get(sid)
            if not s:
                continue

            # Time since last activity
            elapsed = _time.time() - s.last_activity
            if elapsed < 60:
                ago = f"{int(elapsed)}s"
            elif elapsed < 3600:
                ago = f"{int(elapsed) // 60}m"
            else:
                ago = f"{int(elapsed) // 3600}h{int(elapsed) % 3600 // 60:02d}m"

            info = {
                "session_id": s.session_id,
                "name": s.name,
                "registered": s.registered,
                "active": s.active,
                "health": s.health_status,
                "hostname": s.hostname,
                "cwd": s.cwd,
                "tmux_pane": s.tmux_pane,
                "tmux_session": s.tmux_session,
                "tool_calls": s.tool_call_count,
                "last_tool": s.last_tool_name,
                "last_activity_ago": ago,
                "pending_messages": len(s.pending_messages),
                "inbox_pending": sum(1 for item in s.inbox if not item.done),
                "inbox_done": len(s.inbox_done),
                "is_focused": sid == frontend.manager.active_session_id,
                "is_self": sid == session_id,
            }
            if s.agent_metadata:
                info["metadata"] = s.agent_metadata
            sessions.append(info)

        result = {
            "sessions": sessions,
            "count": len(sessions),
            "focused_session": frontend.manager.active_session_id,
        }
        return _attach_messages(json.dumps(result), session)

    def _tool_get_speech_history(args, session_id):
        """Get speech history for the calling session or all sessions."""
        session = _get_session(session_id)
        session.last_tool_name = "get_speech_history"
        session.tool_call_count += 1

        lines = int(args.get("lines", 30))
        target = args.get("session", "self")  # "self", "all", or a session_id

        result = {}

        if target == "all":
            for sid in frontend.manager.session_order:
                s = frontend.manager.sessions.get(sid)
                if not s:
                    continue
                entries = []
                for entry in s.speech_log[-lines:]:
                    entries.append({
                        "time": entry.timestamp,
                        "text": entry.text[:300],
                    })
                result[s.name or sid] = entries
        else:
            # Get speech log for the specified or calling session
            if target == "self":
                s = session
            else:
                s = frontend.manager.sessions.get(target, session)

            entries = []
            for entry in s.speech_log[-lines:]:
                entries.append({
                    "time": entry.timestamp,
                    "text": entry.text[:300],
                })

            # Also include selections from history
            selections = []
            for entry in s.history[-lines:]:
                selections.append({
                    "time": entry.timestamp,
                    "preamble": entry.preamble[:200] if entry.preamble else "",
                    "selected": entry.label[:200] if entry.label else "",
                })

            result = {
                "speech": entries,
                "selections": selections,
                "session": s.name or s.session_id,
            }

        return _attach_messages(json.dumps(result), session)

    def _tool_get_current_choices(args, session_id):
        """Get the choices currently being displayed to the user."""
        session = _get_session(session_id)
        session.last_tool_name = "get_current_choices"
        session.tool_call_count += 1

        target = args.get("session", "focused")  # "focused" or a session_id

        if target == "focused":
            s = frontend.manager.focused()
        else:
            s = frontend.manager.sessions.get(target)

        if not s:
            return _attach_messages(json.dumps({
                "error": "Session not found",
                "session": target,
            }), session)

        result = {
            "session": s.name or s.session_id,
            "active": s.active,
            "preamble": s.preamble,
            "choices": s.choices,
            "pending_inbox": [
                {
                    "preamble": item.preamble or item.text,
                    "kind": item.kind,
                    "n_choices": len(item.choices),
                    "done": item.done,
                }
                for item in s.inbox
            ],
        }

        return _attach_messages(json.dumps(result), session)

    def _tool_get_tui_state(args, session_id):
        """Capture the current TUI screen content and UI state."""
        session = _get_session(session_id)
        session.last_tool_name = "get_tui_state"
        session.tool_call_count += 1

        state = {}

        # Focused session info
        focused = frontend.manager.focused()
        state["focused_session"] = focused.name if focused else None
        state["session_count"] = frontend.manager.count()

        # Current UI mode
        app = frontend._app
        state["ui_mode"] = "idle"
        if hasattr(app, '_in_settings') and app._in_settings:
            state["ui_mode"] = "settings"
        elif hasattr(app, '_filter_mode') and app._filter_mode:
            state["ui_mode"] = "filter"
        elif hasattr(app, '_message_mode') and app._message_mode:
            state["ui_mode"] = "message_input"
        elif hasattr(app, '_conversation_mode') and app._conversation_mode:
            state["ui_mode"] = "conversation"
        elif focused and focused.voice_recording:
            state["ui_mode"] = "voice_recording"
        elif focused and focused.input_mode:
            state["ui_mode"] = "freeform_input"
        elif focused and focused.active:
            state["ui_mode"] = "choices"
        elif focused:
            state["ui_mode"] = "waiting"

        # Try to capture the TUI screen as text via tmux capture-pane
        # (the TUI runs in a tmux pane)
        screen_text = ""
        try:
            import subprocess
            # Try to capture the TUI's own tmux pane
            result = subprocess.run(
                ["tmux", "capture-pane", "-p", "-S", "-80"],
                capture_output=True, text=True, timeout=3,
            )
            if result.returncode == 0:
                screen_text = result.stdout
        except Exception:
            pass

        if screen_text:
            state["screen"] = screen_text
        else:
            # Fallback: reconstruct from widget state
            try:
                preamble_w = app.query_one("#preamble")
                if preamble_w.display:
                    state["preamble_text"] = str(preamble_w.renderable) if hasattr(preamble_w, 'renderable') else ""
            except Exception:
                pass

            try:
                status_w = app.query_one("#status")
                if status_w.display:
                    state["status_text"] = str(status_w.renderable) if hasattr(status_w, 'renderable') else ""
            except Exception:
                pass

            # List visible choices
            try:
                from .tui.widgets import ChoiceItem
                list_view = app.query_one("#choices")
                if list_view.display:
                    visible_items = []
                    for child in list_view.children:
                        if isinstance(child, ChoiceItem):
                            visible_items.append({
                                "label": child.choice_label,
                                "summary": child.choice_summary,
                                "index": child.choice_index,
                            })
                    state["visible_choices"] = visible_items
            except Exception:
                pass

        # Tab bar info
        tab_names = []
        for sid in frontend.manager.session_order:
            s = frontend.manager.sessions.get(sid)
            if s:
                prefix = "→ " if sid == frontend.manager.active_session_id else "  "
                tab_names.append(f"{prefix}{s.name}")
        state["tabs"] = tab_names

        return _attach_messages(json.dumps(state), session)

    # ─── Dispatch table ───────────────────────────────────────

    TOOLS = {
        "present_choices": _tool_present_choices,
        "present_multi_select": _tool_present_multi_select,
        "speak": _tool_speak,
        "speak_async": _tool_speak_async,
        "speak_urgent": _tool_speak_urgent,
        "set_speed": _tool_set_speed,
        "set_voice": _tool_set_voice,
        "set_tts_model": _tool_set_tts_model,
        "set_stt_model": _tool_set_stt_model,
        "set_emotion": _tool_set_emotion,
        "get_settings": _tool_get_settings,
        "register_session": _tool_register_session,
        "rename_session": _tool_rename_session,
        "reload_config": _tool_reload_config,
        "pull_latest": _tool_pull_latest,
        "run_command": _tool_run_command,
        "request_close": _tool_request_close,
        "request_restart": _tool_request_restart,
        "request_proxy_restart": _tool_request_proxy_restart,
        "check_inbox": _tool_check_inbox,
        "report_status": _tool_report_status,
        "get_logs": _tool_get_logs,
        "get_sessions": _tool_get_sessions,
        "get_speech_history": _tool_get_speech_history,
        "get_current_choices": _tool_get_current_choices,
        "get_tui_state": _tool_get_tui_state,
    }

    # Tools that should NOT get speech reminders (they ARE speech)
    _SPEECH_TOOLS = {"speak", "speak_async", "speak_urgent",
                     "present_choices", "present_multi_select"}

    # Tools that are just metadata/status queries — don't clutter the activity log
    _QUIET_TOOLS = {"check_inbox", "get_logs", "get_sessions",
                    "get_speech_history", "get_current_choices", "get_tui_state",
                    "get_settings", "report_status"}

    def dispatch(tool_name: str, args: dict, session_id: str) -> str:
        handler = TOOLS.get(tool_name)
        if handler is None:
            return json.dumps({"error": f"Unknown tool: {tool_name}"})
        try:
            result = handler(args, session_id)

            # Log activity (skip quiet/meta tools to keep the feed useful)
            if tool_name not in _QUIET_TOOLS:
                session = frontend.manager.get(session_id)
                if session:
                    detail = ""
                    kind = "tool"
                    if tool_name in ("speak", "speak_async", "speak_urgent"):
                        detail = args.get("text", "")[:80]
                        kind = "speech"
                    elif tool_name == "present_choices":
                        detail = args.get("preamble", "")[:80]
                        kind = "choices"
                    elif tool_name == "present_multi_select":
                        detail = args.get("preamble", "")[:80]
                        kind = "choices"
                    elif tool_name == "register_session":
                        detail = args.get("name", args.get("cwd", ""))[:80]
                        kind = "status"
                    elif tool_name == "rename_session":
                        detail = args.get("name", "")[:80]
                    elif tool_name == "run_command":
                        detail = args.get("command", "")[:80]
                    elif tool_name in ("set_speed", "set_voice", "set_emotion",
                                       "set_tts_model", "set_stt_model"):
                        # Extract the value being set
                        for k, v in args.items():
                            detail = str(v)[:40]
                            break
                        kind = "settings"
                    session.log_activity(tool_name, detail, kind)

                    # Check for achievements
                    new_achievements = session.check_achievements()
                    for ach in new_achievements:
                        try:
                            frontend.tts.play_chime("achievement")
                            frontend.tts.speak_async(f"Achievement unlocked: {ach}")
                        except Exception:
                            _file_log.debug("Achievement chime/speech failed", exc_info=True)

                    # Update TUI waiting view to reflect new activity
                    try:
                        frontend.update_tab_bar()
                    except Exception:
                        _file_log.debug("Post-tool tab bar update failed", exc_info=True)

            # Add speech reminder for non-speech tools
            if tool_name not in _SPEECH_TOOLS:
                session = frontend.manager.get(session_id)
                if session:
                    result += _speech_reminder(session)
            return result
        except Exception as e:
            log.error(f"Tool {tool_name} error: {e}")
            _tool_log.error(
                "Tool %s failed: %s", tool_name, e,
                exc_info=True,
                extra={"context": log_context(tool_name=tool_name)},
            )
            try:
                frontend.tts.play_chime("error")
                frontend.tts.speak_async(f"Tool error: {tool_name}. {str(e)[:80]}")
            except Exception:
                _file_log.debug("Failed to play error chime/speech", exc_info=True)
            error_data = {
                "error": f"{type(e).__name__}: {str(e)[:200]}",
                "tool": tool_name,
                "suggestion": "Retry the tool call, or call get_logs() to inspect recent errors.",
            }
            # Include crash log content so agents can self-heal
            crash_log = ""
            from .logging import read_log_tail, TOOL_ERROR_LOG as _tel
            tail_lines = read_log_tail(_tel, 50)
            if tail_lines:
                tail = "\n".join(tail_lines)[-1500:]
                crash_log = (
                    "\n\n[IO-MCP ERROR LOG]\n" + tail
                    + "\n\n[SELF-HEALING: If this is a code bug in io-mcp, "
                    "fix it and call pull_latest() to apply. Source: src/io_mcp/]"
                )
            return json.dumps(error_data) + crash_log

    return dispatch


# ─── Server subcommand ────────────────────────────────────────────

def _run_server_command(args) -> None:
    """Run the MCP proxy server (io-mcp server)."""
    from .proxy import run_proxy_server

    backend_url = f"http://{args.io_mcp_address}"
    print(f"  io-mcp server: proxy on {args.host}:{args.port} → backend {backend_url}", flush=True)

    run_proxy_server(
        host=args.host,
        port=args.port,
        backend_url=backend_url,
        foreground=True,
    )


def _run_status_command() -> None:
    """Show status of proxy and backend processes."""
    import time as _time
    import socket
    import urllib.request
    import urllib.error
    from .proxy import proxy_health

    print("io-mcp status")
    print("─" * 50)

    # ── Check proxy (comprehensive: PID + TCP port) ───────
    proxy = proxy_health(f"localhost:{DEFAULT_PROXY_PORT}")
    proxy_alive = proxy["pid_alive"]
    proxy_port_open = proxy["port_open"]

    if proxy["status"] == "healthy":
        uptime_str = f", up {proxy['uptime']}" if proxy.get("uptime") else ""
        print(f"  Proxy:    ✔ running (PID {proxy['pid']}, :{DEFAULT_PROXY_PORT}{uptime_str})")
    elif proxy["status"] == "degraded":
        print(f"  Proxy:    ⚠ {proxy['details']}")
    else:
        print(f"  Proxy:    ✘ not running (port {DEFAULT_PROXY_PORT})")

    # ── Check backend (PID + health endpoint) ──────────────
    backend_pid = None
    backend_alive = False
    backend_uptime = ""
    try:
        with open(PID_FILE, "r") as f:
            backend_pid = int(f.read().strip())
        os.kill(backend_pid, 0)
        backend_alive = True
        backend_uptime = _format_uptime(os.path.getmtime(PID_FILE))
    except (FileNotFoundError, ValueError, ProcessLookupError, PermissionError):
        pass

    backend_healthy = False
    backend_sessions = 0
    if backend_alive:
        print(f"  Backend:  ✔ running (PID {backend_pid}, :{DEFAULT_BACKEND_PORT}, up {backend_uptime})")
        try:
            url = f"http://localhost:{DEFAULT_BACKEND_PORT}/health"
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=2) as resp:
                backend_healthy = resp.status == 200
        except Exception:
            pass
        if backend_healthy:
            print(f"  Health:   ✔ /health responding")
        else:
            print(f"  Health:   ✘ /health not responding")
    else:
        print(f"  Backend:  ✘ not running (port {DEFAULT_BACKEND_PORT})")

    # ── Check Frontend API + sessions ──────────────────────
    api_healthy = False
    api_data = {}
    try:
        url = f"http://localhost:{DEFAULT_API_PORT}/api/health"
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=2) as resp:
            api_healthy = resp.status == 200
            try:
                api_data = json.loads(resp.read().decode())
            except Exception:
                pass
    except Exception:
        pass

    if api_healthy:
        session_count = api_data.get("sessions", 0)
        sse_subs = api_data.get("sse_subscribers", 0)
        parts = [f"✔ API on :{DEFAULT_API_PORT}"]
        parts.append(f"{session_count} session{'s' if session_count != 1 else ''}")
        if sse_subs > 0:
            parts.append(f"{sse_subs} SSE client{'s' if sse_subs != 1 else ''}")
        print(f"  Frontend: {', '.join(parts)}")
    else:
        print(f"  Frontend: ✘ API not responding (port {DEFAULT_API_PORT})")

    # ── Active sessions detail ─────────────────────────────
    if api_healthy:
        try:
            url = f"http://localhost:{DEFAULT_API_PORT}/api/sessions"
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=2) as resp:
                data = json.loads(resp.read().decode())
                sessions = data.get("sessions", [])
                if sessions:
                    print(f"\n  Active sessions:")
                    for s in sessions:
                        name = s.get("name", "unnamed")
                        sid = s.get("id", "?")[:8]
                        active = "●" if s.get("active") else "○"
                        choices_count = len(s.get("choices", []))
                        status = f"{choices_count} choices" if s.get("active") else "waiting"
                        print(f"    {active} {name} ({sid}…) — {status}")
        except Exception:
            pass

    # ── Summary ────────────────────────────────────────────
    print("─" * 50)
    if proxy_alive and proxy_port_open and backend_alive and backend_healthy:
        print("  All systems operational ✔")
    elif proxy_alive and not backend_alive:
        print("  Proxy OK, backend down — run: io-mcp")
    elif not proxy_alive and not backend_alive:
        print("  Everything down — run: io-mcp --restart")
    else:
        print("  Partial — check logs at /tmp/io-mcp-*.log")


def _format_uptime(start_time: float) -> str:
    """Format seconds since start_time as a human-readable uptime string."""
    from .proxy import _format_uptime as _fmt_uptime
    import time as _time
    elapsed = _time.time() - start_time
    return _fmt_uptime(elapsed)


def _restart_proxy(proxy_address: str = f"localhost:{DEFAULT_PROXY_PORT}",
                   backend_port: int = DEFAULT_BACKEND_PORT,
                   dev: bool = False) -> bool:
    """Kill the MCP proxy and restart it. Returns True on success.

    This can be called from the TUI (quick settings) or CLI (restart-proxy).
    Agent MCP connections will drop but the proxy comes back immediately.
    """
    import time

    print("  Proxy: killing...", flush=True)

    # Kill by PID file
    try:
        with open(PROXY_PID_FILE, "r") as f:
            pid = int(f.read().strip())
        os.kill(pid, signal.SIGTERM)
        time.sleep(0.3)
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    except (FileNotFoundError, ValueError, ProcessLookupError, PermissionError):
        pass

    # Also kill anything on the proxy port
    _kill_port_holder(DEFAULT_PROXY_PORT)
    time.sleep(0.5)

    # Clean up PID file
    try:
        os.unlink(PROXY_PID_FILE)
    except OSError:
        pass

    # Start fresh proxy
    _ensure_proxy_running(proxy_address, backend_port, dev=dev)

    # Verify it came up
    time.sleep(1.0)
    if _is_proxy_alive():
        print("  Proxy: restarted successfully", flush=True)
        return True
    else:
        print("  Proxy: failed to restart — check /tmp/io-mcp-server.log", flush=True)
        return False


def _restart_proxy_command() -> None:
    """CLI subcommand: io-mcp restart-proxy"""
    print("io-mcp restart-proxy")
    print("─" * 40)
    success = _restart_proxy()
    if success:
        print("  Done. Agents will need to reconnect.")
    else:
        print("  Failed. Check logs.")


# ─── Cache subcommand ─────────────────────────────────────────────

def _collect_warmup_texts(config: IoMcpConfig) -> list[str]:
    """Collect all fixed UI strings that should be pre-cached for TTS.

    Returns a deduplicated list of strings from:
    - Extra option labels (PRIMARY_EXTRAS, SECONDARY_EXTRAS, MORE_OPTIONS_ITEM)
    - Settings menu labels
    - Common UI phrases
    - Number words (for instant-select readout)
    - Speed values (for settings scrolling)
    - Emotion/style names, voice preset names, STT model names
    - Theme names
    """
    from .tui.widgets import PRIMARY_EXTRAS, SECONDARY_EXTRAS, MORE_OPTIONS_ITEM
    from .tui.themes import COLOR_SCHEMES
    from .settings import Settings

    texts: list[str] = []

    # 1. Extra option labels
    for item in PRIMARY_EXTRAS:
        texts.append(item["label"])
    for item in SECONDARY_EXTRAS:
        texts.append(item["label"])
    texts.append(MORE_OPTIONS_ITEM["label"])

    # 2. Settings menu labels
    settings_labels = [
        "Speed", "Agent voice", "UI voice", "Style", "STT model",
        "Local TTS", "Color scheme", "TTS cache", "Close settings",
    ]
    texts.extend(settings_labels)

    # 3. Common UI phrases
    common_phrases = [
        "Settings", "Back to choices", "Back to chat", "Settings closed",
        "Help", "Waiting for agent", "No active sessions", "Connected",
    ]
    texts.extend(common_phrases)

    # 4. Number words for instant-select readout
    number_words = [
        "one", "two", "three", "four", "five",
        "six", "seven", "eight", "nine",
    ]
    texts.extend(number_words)

    # 5. Speed values
    speed_values = [f"{v / 10:.1f}" for v in range(5, 26)]
    texts.extend(speed_values)

    # 6. Settings values from config
    settings = Settings(config)
    texts.extend(settings.get_emotions())
    texts.extend(settings.get_voices())
    texts.extend(settings.get_stt_models())

    # 7. Theme names
    texts.extend(COLOR_SCHEMES.keys())

    # Deduplicate while preserving order
    seen: set[str] = set()
    unique: list[str] = []
    for t in texts:
        if t not in seen:
            seen.add(t)
            unique.append(t)

    return unique


def _run_cache_warmup(verbose: bool = False, dry_run: bool = False) -> None:
    """Pre-generate TTS audio for all fixed UI strings."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    config = IoMcpConfig.load()
    tts = TTSEngine(local=False, config=config)

    texts = _collect_warmup_texts(config)

    # Determine which voice(s) to generate for
    agent_voice = config.tts_voice_preset
    ui_voice = config.tts_ui_voice_preset
    has_separate_ui_voice = ui_voice and ui_voice != agent_voice

    # Build work items: (text, voice_override)
    work_items: list[tuple[str, str | None]] = []
    for t in texts:
        work_items.append((t, None))  # agent voice (default)
    if has_separate_ui_voice:
        for t in texts:
            work_items.append((t, ui_voice))  # UI voice

    # Count already cached
    already_cached = 0
    to_generate: list[tuple[str, str | None]] = []
    for text, voice_override in work_items:
        key = tts._cache_key(text, voice_override=voice_override)
        if key in tts._cache:
            already_cached += 1
        else:
            to_generate.append((text, voice_override))

    total = len(work_items)
    voice_info = f"agent voice: {agent_voice}"
    if has_separate_ui_voice:
        voice_info += f", UI voice: {ui_voice}"

    label = "io-mcp cache warmup (dry run)" if dry_run else "io-mcp cache warmup"
    print(label)
    print(f"─" * 40)
    print(f"  {voice_info}")
    print(f"  Total strings: {len(texts)}")
    print(f"  Total items (with voice variants): {total}")
    print(f"  Already cached: {already_cached}")
    print(f"  To generate: {len(to_generate)}")
    print()

    if verbose and to_generate:
        print("  Items to generate:")
        for text, voice_override in to_generate:
            voice_label = voice_override or "(default)"
            print(f"    • {text!r}  [{voice_label}]")
        print()

    if not to_generate:
        print("  All items already cached. Nothing to do.")
        # Print summary
        count, total_bytes = tts.cache_stats()
        size_str = _format_size(total_bytes)
        print(f"\n  Cache: {count} items ({size_str})")
        return

    if dry_run:
        print(f"  Dry run — skipping generation of {len(to_generate)} items.")
        count, total_bytes = tts.cache_stats()
        size_str = _format_size(total_bytes)
        print(f"  Cache: {count} items ({size_str})")
        return

    # Generate with a thread pool
    completed = 0
    errors = 0

    def _generate_one(item: tuple[str, str | None]) -> bool:
        text, voice_override = item
        try:
            result = tts._generate_to_file_unlocked(
                text, voice_override=voice_override)
            return result is not None
        except Exception:
            return False

    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {pool.submit(_generate_one, item): item for item in to_generate}
        for future in as_completed(futures):
            if future.result():
                completed += 1
            else:
                errors += 1
            done = completed + errors
            # Progress counter
            print(f"\r  Generating: {done}/{len(to_generate)}", end="", flush=True)

    print()  # newline after progress

    # Summary
    count, total_bytes = tts.cache_stats()
    size_str = _format_size(total_bytes)
    print(f"\n  Generated {completed} items ({errors} errors)")
    print(f"  Cache: {count} items ({size_str})")


def _run_cache_status(verbose: bool = False) -> None:
    """Show TTS cache statistics."""
    from .tts import CACHE_DIR
    import datetime

    config = IoMcpConfig.load()
    tts = TTSEngine(local=False, config=config)

    count, total_bytes = tts.cache_stats()
    size_str = _format_size(total_bytes)

    # ── Scan disk cache directory ──
    cache_exists = os.path.isdir(CACHE_DIR)
    disk_files: list[tuple[str, int, float]] = []  # (path, size, mtime)
    disk_total_bytes = 0

    if cache_exists:
        try:
            for entry in os.scandir(CACHE_DIR):
                if entry.is_file() and entry.name.endswith(".wav"):
                    try:
                        st = entry.stat()
                        disk_files.append((entry.path, st.st_size, st.st_mtime))
                        disk_total_bytes += st.st_size
                    except OSError:
                        disk_files.append((entry.path, 0, 0.0))
        except OSError:
            pass

    disk_count = len(disk_files)

    # ── Print report ──
    print("io-mcp cache status")
    print("─" * 40)
    print(f"  Directory: {CACHE_DIR}")
    print(f"  Exists:    {'yes' if cache_exists else 'no'}")
    print(f"  Items:     {count}")
    print(f"  Size:      {size_str}")

    # Disk stats (may differ from in-memory if files exist from previous runs)
    if cache_exists:
        print()
        print("  Disk:")
        print(f"    Files:   {disk_count}")
        print(f"    Size:    {_format_size(disk_total_bytes)}")
        if disk_files:
            avg_size = disk_total_bytes // disk_count
            print(f"    Avg:     {_format_size(avg_size)}/file")
            # Oldest and newest
            oldest = min(disk_files, key=lambda e: e[2])
            newest = max(disk_files, key=lambda e: e[2])
            if oldest[2] > 0:
                oldest_dt = datetime.datetime.fromtimestamp(oldest[2])
                print(f"    Oldest:  {oldest_dt.strftime('%Y-%m-%d %H:%M:%S')}")
            if newest[2] > 0:
                newest_dt = datetime.datetime.fromtimestamp(newest[2])
                print(f"    Newest:  {newest_dt.strftime('%Y-%m-%d %H:%M:%S')}")

    # Current config context
    print()
    print("  Config:")
    print(f"    Voice:   {config.tts_voice_preset}")
    print(f"    Model:   {config.tts_model_name}")
    print(f"    Speed:   {config.tts_speed}")
    if config.tts_emotion:
        print(f"    Emotion: {config.tts_emotion}")
    if config.tts_ui_voice_preset and config.tts_ui_voice_preset != config.tts_voice_preset:
        print(f"    UI voice: {config.tts_ui_voice_preset}")

    # Verbose: show in-memory cache entries (hash → path)
    if verbose and tts._cache:
        print()
        print("  Cached entries:")
        for key, path in sorted(tts._cache.items()):
            try:
                fsize = os.path.getsize(path)
                print(f"    {key[:12]}…  {_format_size(fsize):>10}  {os.path.basename(path)}")
            except OSError:
                print(f"    {key[:12]}…  (missing)")

    # Verbose: show disk file details (sorted newest first)
    if verbose and disk_files:
        print()
        print("  Disk files:")
        disk_files.sort(key=lambda e: e[2], reverse=True)
        for path, fsize, mtime in disk_files:
            name = os.path.basename(path)
            if mtime > 0:
                dt = datetime.datetime.fromtimestamp(mtime)
                time_str = dt.strftime("%m-%d %H:%M")
            else:
                time_str = "unknown"
            print(f"    {name[:16]}…  {_format_size(fsize):>10}  {time_str}")


def _format_size(nbytes: int) -> str:
    """Format byte count as human-readable string."""
    if nbytes >= 1_048_576:
        return f"{nbytes / 1_048_576:.1f} MB"
    elif nbytes >= 1024:
        return f"{nbytes / 1024:.1f} KB"
    else:
        return f"{nbytes} B"


def _run_cache_command() -> None:
    """CLI subcommand: io-mcp cache [warmup|status]"""
    import argparse

    parser = argparse.ArgumentParser(
        prog="io-mcp cache",
        description="Manage TTS audio cache",
    )
    parser.add_argument("cache", help=argparse.SUPPRESS)  # consume 'cache'
    parser.add_argument("action", choices=["warmup", "status"],
                        help="Action: warmup (pre-generate audio) or status (show cache stats)")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Show verbose output (cache entry details for status)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be generated without generating (warmup only)")
    args = parser.parse_args()

    if args.action == "warmup":
        _run_cache_warmup(verbose=args.verbose, dry_run=args.dry_run)
    elif args.action == "status":
        _run_cache_status(verbose=args.verbose)


# ─── Main entry point ────────────────────────────────────────────

def main() -> None:
    # Check for subcommands first (before argparse to avoid conflicts)
    if len(sys.argv) > 1 and sys.argv[1] == "server":
        parser = argparse.ArgumentParser(prog="io-mcp server",
            description="Run the MCP proxy server daemon")
        parser.add_argument("server", help=argparse.SUPPRESS)  # consume 'server'
        parser.add_argument("--host", default="0.0.0.0")
        parser.add_argument("--port", type=int, default=DEFAULT_PROXY_PORT)
        parser.add_argument("--io-mcp-address", default=f"localhost:{DEFAULT_BACKEND_PORT}")
        parser.add_argument("--foreground", action="store_true")
        args = parser.parse_args()
        _run_server_command(args)
        return

    if len(sys.argv) > 1 and sys.argv[1] == "status":
        _run_status_command()
        return

    if len(sys.argv) > 1 and sys.argv[1] == "restart-proxy":
        _restart_proxy_command()
        return

    if len(sys.argv) > 1 and sys.argv[1] == "cache":
        _run_cache_command()
        return

    # ─── Ensure truecolor support ──────────────────────────────
    # tmux and screen strip COLORTERM from the environment, causing
    # Rich/Textual to fall back to 256-color mode.  Our CSS uses hex
    # colors exclusively, so force truecolor so rendering is identical
    # inside and outside tmux.  Also upgrade TERM from "screen*" to
    # "xterm-256color" which modern tmux supports.
    if not os.environ.get("COLORTERM"):
        os.environ["COLORTERM"] = "truecolor"
    term = os.environ.get("TERM", "")
    if term.startswith("screen") or term == "dumb":
        os.environ["TERM"] = "xterm-256color"

    # ─── Main backend ─────────────────────────────────────────
    parser = argparse.ArgumentParser(
        description="io-mcp — scroll-wheel input + TTS narration for Claude Code"
    )
    parser.add_argument("--local", action="store_true", help="Use espeak-ng instead of API TTS")
    parser.add_argument("--port", type=int, default=DEFAULT_BACKEND_PORT,
                        help=f"Backend port (default: {DEFAULT_BACKEND_PORT})")
    parser.add_argument("--host", default="0.0.0.0", help="Bind address")
    parser.add_argument("--proxy-address", default=f"localhost:{DEFAULT_PROXY_PORT}",
                        help=f"MCP proxy address (default: localhost:{DEFAULT_PROXY_PORT})")
    parser.add_argument("--dwell", type=float, default=0.0, metavar="SECONDS")
    parser.add_argument("--scroll-debounce", type=float, default=0.15, metavar="SECONDS")
    parser.add_argument("--append-option", action="append", default=[], metavar="LABEL")
    parser.add_argument("--append-silent-option", action="append", default=[], metavar="LABEL")
    parser.add_argument("--demo", action="store_true", help="Demo mode")
    parser.add_argument("--dev", action="store_true",
                        help="Dev mode: use 'uv run python -m io_mcp' for proxy subprocess")
    parser.add_argument("--restart", action="store_true",
                        help="Force kill all io-mcp processes before starting (cleans up stale ports)")
    parser.add_argument("--freeform-tts", choices=["api", "local"], default="local")
    parser.add_argument("--freeform-tts-speed", type=float, default=1.6, metavar="SPEED")
    parser.add_argument("--freeform-tts-delimiters", default=" .,;:!?")
    parser.add_argument("--invert", action="store_true")
    parser.add_argument("--config-file", default=None, metavar="PATH")
    parser.add_argument("--default-config", action="store_true",
                        help="Ignore user config, use built-in defaults (does not overwrite config file)")
    parser.add_argument("--reset-config", action="store_true",
                        help="Delete config.yml and regenerate with current defaults (clean slate)")
    parser.add_argument("--djent", action="store_true")
    args = parser.parse_args()

    # No default append options — "More options" is handled by the TUI's
    # collapsed extras toggle and shouldn't appear as a numbered choice.
    # (Previously defaulted to ["More options"] which duplicated the TUI toggle.)

    if args.default_config:
        # Use built-in defaults only — don't read or write user config
        import copy
        from .config import DEFAULT_CONFIG, _expand_config
        raw = copy.deepcopy(DEFAULT_CONFIG)
        expanded = _expand_config(raw)
        config = IoMcpConfig(raw=raw, expanded=expanded, config_path="/dev/null")
        print("  Config: using built-in defaults (--default-config)", flush=True)
    elif args.reset_config:
        # Delete and regenerate config with all current defaults
        config = IoMcpConfig.reset(args.config_file)
        print("  Config: reset to defaults (--reset-config)", flush=True)
    else:
        config = IoMcpConfig.load(args.config_file)
    if args.djent:
        config.djent_enabled = True

    print(f"  Config: {config.config_path}", flush=True)
    print(f"  TTS: model={config.tts_model_name}, voice={config.tts_voice}, speed={config.tts_speed}", flush=True)
    print(f"  STT: model={config.stt_model_name}, realtime={config.stt_realtime}", flush=True)
    if config.djent_enabled:
        print(f"  Djent: enabled", flush=True)

    tts = TTSEngine(local=args.local, config=config)
    freeform_local = args.freeform_tts == "local"
    freeform_tts = TTSEngine(local=freeform_local, speed=args.freeform_tts_speed, config=config)

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
        def _demo_loop():
            import time
            time.sleep(0.5)
            demo_session, _ = app.manager.get_or_create("demo")
            demo_session.name = "Demo"
            round_num = 0
            while True:
                round_num += 1
                choices = [
                    {"label": "Fix the bug", "summary": "Null pointer in auth module"},
                    {"label": "Run the tests", "summary": "Execute test suite"},
                    {"label": "Show the diff", "summary": "Changes since last commit"},
                    {"label": "Deploy to staging", "summary": "Push to staging env"},
                ]
                for opt in args.append_option:
                    t, d = (opt.split("::", 1) + [""])[:2]
                    if not any(c["label"].lower() == t.lower() for c in choices):
                        choices.append({"label": t, "summary": d})
                result = app.present_choices(demo_session, f"Demo round {round_num}.", choices)
                if result.get("selected") == "quit":
                    break
                time.sleep(0.3)
        threading.Thread(target=_demo_loop, daemon=True).start()
    else:
        # --restart: force kill everything first
        if args.restart:
            _force_kill_all()

        _kill_existing_backend()
        _write_pid_file()
        atexit.register(_remove_pid_file)

        _acquire_wake_lock()
        atexit.register(_release_wake_lock)

        # Ensure proxy daemon is running
        _ensure_proxy_running(args.proxy_address, args.port, dev=args.dev)

        # Create tool dispatcher and start backend HTTP server.
        # app_ref is a mutable list so the dispatcher always uses the current app
        # (survives TUI restarts without restarting the backend server).
        app_ref = [app]
        dispatch = _create_tool_dispatcher(app_ref, args.append_option, args.append_silent_option or [])

        from .backend import start_backend_server

        def _cancel_dispatch(tool_name: str, session_id: str) -> None:
            """Cancel a pending tool call — resolves inbox items with _cancelled."""
            try:
                _app = app_ref[0]
                session = _app.manager.get(session_id)
                if not session:
                    return
                # Cancel the front inbox item if it matches the tool
                front = session.peek_inbox()
                if front and not front.done:
                    front.result = {"selected": "_cancelled", "summary": f"Cancelled by client"}
                    front.done = True
                    front.event.set()
                    session.drain_kick.set()
                    # Update UI
                    _app.call_from_thread(_app._update_inbox_list)
                    _app.call_from_thread(_app._update_tab_bar)
            except Exception:
                _file_log.debug("cancel_mcp handler failed", exc_info=True)

        def _report_activity(session_id: str, tool: str, detail: str, kind: str) -> None:
            """Log an activity from a hook (e.g. PreToolUse) to the session's feed."""
            try:
                _app = app_ref[0]
                # Find the session — hooks may send Claude's session_id which
                # doesn't match the MCP session_id. Try all sessions.
                session = _app.manager.get(session_id)
                if not session:
                    # Try finding by matching any active session
                    sessions = _app.manager.all_sessions()
                    if sessions:
                        session = sessions[-1]  # Use most recent
                if session:
                    session.log_activity(tool, detail, kind)
            except Exception:
                _file_log.debug("report_activity failed", exc_info=True)

        start_backend_server(dispatch, host="0.0.0.0", port=args.port,
                           cancel_dispatch=_cancel_dispatch,
                           report_activity=_report_activity)
        print(f"  Backend: /handle-mcp on 0.0.0.0:{args.port}", flush=True)

        # Start Android SSE API on :8445
        try:
            from .api import start_api_server
            from .tui import EXTRA_OPTIONS

            class _ApiFrontend:
                @property
                def manager(self):
                    return app_ref[0].manager
                @property
                def config(self):
                    return app_ref[0]._config

            def _on_highlight(session_id: str, choice_index: int):
                _app = app_ref[0]
                session = _app.manager.get(session_id)
                if not session or not session.active:
                    return
                extras = getattr(session, 'extras_count', len(EXTRA_OPTIONS))
                display_idx = extras + (choice_index - 1)
                def _set():
                    try:
                        from textual.widgets import ListView
                        lv = _app.query_one("#choices", ListView)
                        if lv.display and 0 <= display_idx < len(lv.children):
                            lv.index = display_idx
                    except Exception:
                        pass
                try:
                    _app.call_from_thread(_set)
                except Exception:
                    pass

            def _on_key(session_id: str, key: str):
                _app = app_ref[0]
                def _do():
                    try:
                        {"j": _app.action_cursor_down, "k": _app.action_cursor_up,
                         "enter": _app.action_select, "space": _app.action_voice_input,
                         "u": _app.action_undo_selection,
                         "h": _app.action_prev_tab, "l": _app.action_next_tab,
                         "s": _app.action_toggle_settings, "d": _app.action_dashboard,
                         "n": _app.action_next_choices_tab,
                         "m": _app.action_queue_message, "M": _app.action_voice_message,
                         "i": _app.action_freeform_input,
                        }.get(key, lambda: None)()
                    except Exception:
                        pass
                try:
                    _app.call_from_thread(_do)
                except Exception:
                    pass

            start_api_server(_ApiFrontend(), port=DEFAULT_API_PORT, host=args.host,
                           highlight_callback=_on_highlight, key_callback=_on_key)
            print(f"  Android API: SSE on {args.host}:{DEFAULT_API_PORT}", flush=True)
        except Exception as e:
            print(f"  Android API: failed — {e}", flush=True)

    # Start ring receiver (UDP listener for ring-mods smart ring events)
    # Disabled by default — enable in config.yml: config.ringReceiver.enabled: true
    ring_receiver = None
    if config.ring_receiver_enabled:
        try:
            from .ring_receiver import RingReceiver

            def _ring_key(key: str):
                """Handle key events from the smart ring via UDP."""
                log.info("Ring UDP event: key=%s", key)
                _app = app_ref[0]
                def _do():
                    try:
                        {"j": _app.action_cursor_down, "k": _app.action_cursor_up,
                         "enter": _app.action_select, "space": _app.action_voice_input,
                         "u": _app.action_undo_selection,
                         "h": _app.action_prev_tab, "l": _app.action_next_tab,
                         "s": _app.action_toggle_settings, "d": _app.action_dashboard,
                         "n": _app.action_next_choices_tab,
                         "m": _app.action_queue_message, "M": _app.action_voice_message,
                         "i": _app.action_freeform_input,
                        }.get(key, lambda: None)()
                    except Exception:
                        pass
                try:
                    _app.call_from_thread(_do)
                except Exception:
                    pass

            ring_port = config.ring_receiver_port
            ring_receiver = RingReceiver(callback=_ring_key, port=ring_port)
            ring_receiver.start()
            print(f"  Ring receiver: UDP :{ring_port} (ring-mods input)", flush=True)
        except Exception as e:
            print(f"  Ring receiver: failed — {e}", flush=True)

    # Run the TUI in a restart loop.
    # Exit code 42 means "restart", anything else means "quit for real".
    RESTART_CODE = 42
    while True:
        app.run()
        if getattr(app, '_restart_requested', False):
            print("\n  Restarting TUI...", flush=True)
            # Re-create the TUI app for a clean restart
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
            # Update the mutable reference so the backend server
            # dispatches to the new app instance
            app_ref[0] = app
            continue
        break


if __name__ == "__main__":
    main()
