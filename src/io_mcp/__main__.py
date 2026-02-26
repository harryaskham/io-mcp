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
from .logging import get_logger, log_context, TUI_ERROR_LOG, TOOL_ERROR_LOG, SERVER_LOG

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
        pass

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
                pass
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

    # ─── Tool implementations ─────────────────────────────────

    def _tool_present_choices(args, session_id):
        session = _get_session(session_id)
        session.last_tool_name = "present_choices"
        preamble = args.get("preamble", "")
        choices = args.get("choices", [])
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
            result = frontend.present_choices(session, preamble, all_choices)
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
        text = args.get("text", "")
        # Enqueue as inbox item — agent returns immediately
        item = session.enqueue_speech(text, blocking=False, priority=0)
        frontend.notify_inbox_update(session)
        preview = text[:100] + ("..." if len(text) > 100 else "")
        return _attach_messages(f"Spoke: {preview}", session)

    def _tool_speak_urgent(args, session_id):
        session = _get_session(session_id)
        session.last_tool_name = "speak_urgent"
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
            return json.dumps({
                "tts_voice": frontend.config.tts_voice_preset,
                "tts_model": frontend.config.tts_model_name,
                "tts_speed": frontend.config.tts_speed,
                "tts_style": frontend.config.tts_style,
                "voices": frontend.config.voice_preset_names,
                "styles": frontend.config.tts_style_options,
                "stt_model": frontend.config.stt_model_name,
                "stt_models": frontend.config.stt_model_names,
            })
        return json.dumps({"error": "No config available"})

    def _tool_register_session(args, session_id):
        session = _get_session(session_id)
        session.last_tool_name = "register_session"
        session.registered = True
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
        result = frontend.present_choices(session,
            "Agent requests TUI restart. Sessions reset, MCP proxy stays alive.",
            [{"label": "Approve restart", "summary": "Restart io-mcp TUI now"},
             {"label": "Deny", "summary": "Keep running"}])
        if result.get("selected", "").lower() != "approve restart":
            return json.dumps({"status": "rejected", "message": "User denied restart"})

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
                pass

        threading.Thread(target=_do_restart, daemon=True).start()
        return json.dumps({"status": "accepted", "message": "TUI will restart in ~1.5 seconds. Proxy stays alive."})

    def _tool_request_proxy_restart(args, session_id):
        session = _get_session(session_id)
        session.last_tool_name = "request_proxy_restart"
        result = frontend.present_choices(session,
            "Agent requests PROXY restart. This will break ALL agent MCP connections. They must reconnect.",
            [{"label": "Approve proxy restart", "summary": "Restart the MCP proxy — all agents disconnect"},
             {"label": "Deny", "summary": "Keep proxy running"}])
        if result.get("selected", "").lower() != "approve proxy restart":
            return json.dumps({"status": "rejected", "message": "User denied proxy restart"})

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
        return json.dumps({"status": "accepted", "message": "Proxy restarting. Agents must reconnect."})

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
                frontend.manager.remove(session_id)
                try:
                    frontend.update_tab_bar()
                except Exception:
                    pass
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
        "get_logs": _tool_get_logs,
        "get_sessions": _tool_get_sessions,
        "get_speech_history": _tool_get_speech_history,
        "get_current_choices": _tool_get_current_choices,
        "get_tui_state": _tool_get_tui_state,
    }

    # Tools that should NOT get speech reminders (they ARE speech)
    _SPEECH_TOOLS = {"speak", "speak_async", "speak_urgent",
                     "present_choices", "present_multi_select"}

    def dispatch(tool_name: str, args: dict, session_id: str) -> str:
        handler = TOOLS.get(tool_name)
        if handler is None:
            return json.dumps({"error": f"Unknown tool: {tool_name}"})
        try:
            result = handler(args, session_id)
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
                pass
            error_data = {"error": f"{type(e).__name__}: {str(e)[:200]}", "tool": tool_name}
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
    import urllib.request
    import urllib.error

    print("io-mcp status")
    print("─" * 40)

    # Check proxy
    proxy_pid = None
    proxy_alive = False
    try:
        with open(PROXY_PID_FILE, "r") as f:
            proxy_pid = int(f.read().strip())
        os.kill(proxy_pid, 0)
        proxy_alive = True
    except (FileNotFoundError, ValueError, ProcessLookupError, PermissionError):
        pass

    if proxy_alive:
        print(f"  Proxy:   ✔ running (PID {proxy_pid}, port {DEFAULT_PROXY_PORT})")
    else:
        print(f"  Proxy:   ✘ not running (port {DEFAULT_PROXY_PORT})")

    # Check backend
    backend_pid = None
    backend_alive = False
    try:
        with open(PID_FILE, "r") as f:
            backend_pid = int(f.read().strip())
        os.kill(backend_pid, 0)
        backend_alive = True
    except (FileNotFoundError, ValueError, ProcessLookupError, PermissionError):
        pass

    if backend_alive:
        print(f"  Backend: ✔ running (PID {backend_pid}, port {DEFAULT_BACKEND_PORT})")
    else:
        print(f"  Backend: ✘ not running (port {DEFAULT_BACKEND_PORT})")

    # Check backend health endpoint
    backend_healthy = False
    if backend_alive:
        try:
            url = f"http://localhost:{DEFAULT_BACKEND_PORT}/health"
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=2) as resp:
                backend_healthy = resp.status == 200
        except Exception:
            pass
        if backend_healthy:
            print(f"  Backend: ✔ /health responding")
        else:
            print(f"  Backend: ✘ /health not responding")

    # Check Android API
    api_healthy = False
    try:
        url = f"http://localhost:{DEFAULT_API_PORT}/api/health"
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=2) as resp:
            api_healthy = resp.status == 200
    except Exception:
        pass

    if api_healthy:
        print(f"  Android: ✔ API on port {DEFAULT_API_PORT}")
    else:
        print(f"  Android: ✘ API not responding (port {DEFAULT_API_PORT})")

    print("─" * 40)
    if proxy_alive and backend_alive and backend_healthy:
        print("  All systems operational")
    elif proxy_alive and not backend_alive:
        print("  Proxy OK, backend down — run: io-mcp")
    elif not proxy_alive and not backend_alive:
        print("  Everything down — run: io-mcp --restart")
    else:
        print("  Partial — check logs at /tmp/io-mcp-*.log")


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
        start_backend_server(dispatch, host="0.0.0.0", port=args.port)
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
