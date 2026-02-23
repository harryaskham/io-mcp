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

log = logging.getLogger("io_mcp")

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


# ─── Backend tool dispatcher ──────────────────────────────────────────

def _create_tool_dispatcher(app: IoMcpApp, append_options: list[str],
                            append_silent_options: list[str]):
    """Create a function that dispatches tool calls to the app.

    Returns a callable(tool_name, args, session_id) -> str
    """
    # Adapt IoMcpApp to the Frontend protocol
    class _AppFrontend:
        @property
        def manager(self):
            return app.manager
        @property
        def config(self):
            return app._config
        @property
        def tts(self):
            return app._tts

        def present_choices(self, session, preamble, choices):
            return app.present_choices(session, preamble, choices)
        def present_multi_select(self, session, preamble, choices):
            return app.present_multi_select(session, preamble, choices)
        def session_speak(self, session, text, block=True, priority=0, emotion=""):
            return app.session_speak(session, text, block, priority, emotion)
        def session_speak_async(self, session, text):
            return app.session_speak(session, text, block=False)
        def on_session_created(self, session):
            return app.on_session_created(session)
        def update_tab_bar(self):
            app.call_from_thread(app._update_tab_bar)
        def hot_reload(self):
            app.call_from_thread(app.action_hot_reload)

    frontend = _AppFrontend()

    import time as _time

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
            if not any(c.get("label", "").lower() == title.lower() for c in all_choices):
                all_choices.append({"label": title, "summary": desc})
        for opt in append_silent_options:
            if "::" in opt:
                title, desc = opt.split("::", 1)
            else:
                title, desc = opt, ""
            if not any(c.get("label", "").lower() == title.lower() for c in all_choices):
                all_choices.append({"label": title, "summary": desc, "_silent": True})
        for opt in _config_extras:
            if not any(c.get("label", "").lower() == opt["label"].lower() for c in all_choices):
                all_choices.append(dict(opt))

        while True:
            session.last_preamble = preamble
            session.last_choices = list(all_choices)
            result = frontend.present_choices(session, preamble, all_choices)
            if result.get("selected") == "_undo":
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
        result = frontend.present_multi_select(session, preamble, list(choices))
        return _attach_messages(json.dumps({"selected": result}), session)

    def _tool_speak(args, session_id):
        session = _get_session(session_id)
        session.last_tool_name = "speak"
        text = args.get("text", "")
        frontend.session_speak(session, text, True, 0, "")
        preview = text[:100] + ("..." if len(text) > 100 else "")
        return _attach_messages(f"Spoke: {preview}", session)

    def _tool_speak_async(args, session_id):
        session = _get_session(session_id)
        session.last_tool_name = "speak_async"
        text = args.get("text", "")
        frontend.session_speak(session, text, False, 0, "")
        preview = text[:100] + ("..." if len(text) > 100 else "")
        return _attach_messages(f"Spoke: {preview}", session)

    def _tool_speak_urgent(args, session_id):
        session = _get_session(session_id)
        session.last_tool_name = "speak_urgent"
        text = args.get("text", "")
        frontend.session_speak(session, text, True, 1, "")
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
            frontend.config.set_tts_voice(voice)
            frontend.config.save()
            frontend.tts.clear_cache()
        return f"Voice set to {voice}"

    def _tool_set_tts_model(args, session_id):
        model = args.get("model", "")
        if frontend.config:
            frontend.config.set_tts_model(model)
            frontend.config.save()
            frontend.tts.clear_cache()
            return f"TTS model set to {model}, voice reset to {frontend.config.tts_voice}"
        return f"TTS model set to {model}"

    def _tool_set_stt_model(args, session_id):
        model = args.get("model", "")
        if frontend.config:
            frontend.config.set_stt_model(model)
            frontend.config.save()
        return f"STT model set to {model}"

    def _tool_set_emotion(args, session_id):
        emotion = args.get("emotion", "")
        if frontend.config:
            frontend.config.set_tts_emotion(emotion)
            frontend.config.save()
            frontend.tts.clear_cache()
        return f"Emotion set to: {emotion}"

    def _tool_get_settings(args, session_id):
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

    def _tool_register_session(args, session_id):
        session = _get_session(session_id)
        session.last_tool_name = "register_session"
        session.registered = True
        for field in ("cwd", "hostname", "tmux_session", "tmux_pane"):
            val = args.get(field, "")
            if val:
                setattr(session, field, val)
        if args.get("name"):
            session.name = args["name"]
        if args.get("voice"):
            session.voice_override = args["voice"]
        if args.get("emotion"):
            session.emotion_override = args["emotion"]
        if args.get("metadata"):
            session.agent_metadata.update(args["metadata"])

        import socket
        local_hostname = socket.gethostname()
        hostname = args.get("hostname", "")
        is_local = (hostname == local_hostname) if hostname else True

        try:
            frontend.update_tab_bar()
        except Exception:
            pass
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
                "tts_model": frontend.config.tts_model_name,
                "tts_voice": frontend.config.tts_voice,
                "tts_speed": frontend.config.tts_speed,
                "tts_emotion": frontend.config.tts_emotion,
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
                return json.dumps({"status": "pulled_and_reloaded", "output": output})
            except Exception:
                return json.dumps({"status": "pulled", "output": output, "note": "Hot reload failed"})
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
            "Agent requests backend restart. TUI/config/code reloads. MCP connection stays alive.",
            [{"label": "Approve restart", "summary": "Restart io-mcp backend now"},
             {"label": "Deny", "summary": "Keep running"}])
        if result.get("selected", "").lower() != "approve restart":
            return json.dumps({"status": "rejected", "message": "User denied restart"})

        def _do_restart():
            import time as _t
            _t.sleep(0.5)
            frontend.tts.speak_async("Restarting backend...")
            _t.sleep(1.0)
            os.execv(sys.executable, [sys.executable, "-m", "io_mcp"] + sys.argv[1:])

        threading.Thread(target=_do_restart, daemon=True).start()
        return json.dumps({"status": "accepted", "message": "Backend will restart in ~1.5 seconds"})

    def _tool_request_proxy_restart(args, session_id):
        session = _get_session(session_id)
        session.last_tool_name = "request_proxy_restart"
        result = frontend.present_choices(session,
            "Agent requests PROXY restart. This will break ALL agent MCP connections. They must reconnect.",
            [{"label": "Approve proxy restart", "summary": "Restart the MCP proxy — all agents disconnect"},
             {"label": "Deny", "summary": "Keep proxy running"}])
        if result.get("selected", "").lower() != "approve proxy restart":
            return json.dumps({"status": "rejected", "message": "User denied proxy restart"})
        return json.dumps({"status": "accepted", "message": "Proxy will restart"})

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
        "request_restart": _tool_request_restart,
        "request_proxy_restart": _tool_request_proxy_restart,
    }

    def dispatch(tool_name: str, args: dict, session_id: str) -> str:
        handler = TOOLS.get(tool_name)
        if handler is None:
            return json.dumps({"error": f"Unknown tool: {tool_name}"})
        try:
            return handler(args, session_id)
        except Exception as e:
            import traceback
            log.error(f"Tool {tool_name} error: {e}")
            try:
                with open("/tmp/io-mcp-tool-error.log", "a") as f:
                    f.write(f"\n--- {tool_name} ---\n{traceback.format_exc()}\n")
            except Exception:
                pass
            try:
                frontend.tts.play_chime("error")
                frontend.tts.speak_async(f"Tool error: {tool_name}. {str(e)[:80]}")
            except Exception:
                pass
            return json.dumps({"error": f"{type(e).__name__}: {str(e)[:200]}", "tool": tool_name})

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
    parser.add_argument("--djent", action="store_true")
    args = parser.parse_args()

    if not args.append_option:
        args.append_option = ["More options"]

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

        # Create tool dispatcher and start backend HTTP server
        dispatch = _create_tool_dispatcher(app, args.append_option, args.append_silent_option or [])

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
                    return app.manager
                @property
                def config(self):
                    return app._config

            def _on_highlight(session_id: str, choice_index: int):
                session = app.manager.get(session_id)
                if not session or not session.active:
                    return
                extras = getattr(session, 'extras_count', len(EXTRA_OPTIONS))
                display_idx = extras + (choice_index - 1)
                def _set():
                    try:
                        from textual.widgets import ListView
                        lv = app.query_one("#choices", ListView)
                        if lv.display and 0 <= display_idx < len(lv.children):
                            lv.index = display_idx
                    except Exception:
                        pass
                try:
                    app.call_from_thread(_set)
                except Exception:
                    pass

            def _on_key(session_id: str, key: str):
                def _do():
                    try:
                        {"j": app.action_cursor_down, "k": app.action_cursor_up,
                         "enter": app.action_select, "space": app.action_voice_input,
                         "u": app.action_undo_selection}.get(key, lambda: None)()
                    except Exception:
                        pass
                try:
                    app.call_from_thread(_do)
                except Exception:
                    pass

            start_api_server(_ApiFrontend(), port=DEFAULT_API_PORT, host=args.host,
                           highlight_callback=_on_highlight, key_callback=_on_key)
            print(f"  Android API: SSE on {args.host}:{DEFAULT_API_PORT}", flush=True)
        except Exception as e:
            print(f"  Android API: failed — {e}", flush=True)

    app.run()


if __name__ == "__main__":
    main()
