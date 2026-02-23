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
import atexit
import logging
import os
import signal
import threading

from mcp.server.fastmcp import Context

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
        from .server import create_mcp_server

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
        server = create_mcp_server(
            frontend, host=host, port=port,
            append_options=append_options,
            append_silent_options=append_silent_options,
        )

        import logging
        # Suppress uvicorn and MCP server logs — they print over the TUI
        for logger_name in ("mcp", "uvicorn", "uvicorn.access", "uvicorn.error", "httpx"):
            _log = logging.getLogger(logger_name)
            _log.setLevel(logging.WARNING)
            _log.handlers = []  # Remove any existing handlers

        with open("/tmp/io-mcp-server.log", "w") as f:
            f.write(f"Starting MCP server on {host}:{port}\n")

        server.run(transport="streamable-http")

    except Exception:
        import traceback
        crash = traceback.format_exc()
        with open("/tmp/io-mcp-crash.log", "w") as f:
            f.write(crash)
        log.error("MCP server thread crashed — see /tmp/io-mcp-crash.log")


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

        # Start MCP streamable-http server in background thread with watchdog
        def _run_with_watchdog():
            """Run the MCP server with auto-restart on crash."""
            import time as _time
            restart_count = 0
            max_restarts = 5
            while restart_count < max_restarts:
                try:
                    _run_mcp_server(app, args.host, args.port,
                                   args.append_option, args.append_silent_option)
                    break  # Normal exit
                except Exception:
                    restart_count += 1
                    import traceback
                    with open("/tmp/io-mcp-crash.log", "a") as f:
                        f.write(f"\n--- Crash #{restart_count} ---\n")
                        f.write(traceback.format_exc())
                    backoff = min(2 ** restart_count, 30)
                    print(f"  MCP server crashed (#{restart_count}), restarting in {backoff}s...", flush=True)
                    _time.sleep(backoff)

        mcp_thread = threading.Thread(target=_run_with_watchdog, daemon=True)
        mcp_thread.start()

        # Start frontend API server for remote clients (Android app, etc.)
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
                """Handle highlight from Android app — update TUI ListView."""
                session = app.manager.get(session_id)
                if not session or not session.active:
                    return
                # Convert 1-based choice index to display index
                # Display index = extras_count + (choice_index - 1)
                extras = getattr(session, 'extras_count', len(EXTRA_OPTIONS))
                display_idx = extras + (choice_index - 1)

                def _set_highlight():
                    try:
                        from .tui import ListView
                        list_view = app.query_one("#choices", ListView)
                        if list_view.display and 0 <= display_idx < len(list_view.children):
                            list_view.index = display_idx
                    except Exception:
                        pass

                try:
                    app.call_from_thread(_set_highlight)
                except Exception:
                    pass

            def _on_key(session_id: str, key: str):
                """Handle key event from Android app — forward to TUI."""
                def _do_key():
                    try:
                        if key == "j":
                            app.action_cursor_down()
                        elif key == "k":
                            app.action_cursor_up()
                        elif key == "enter":
                            app.action_select()
                        elif key == "space":
                            app.action_voice_input()
                        elif key == "u":
                            app.action_undo_selection()
                    except Exception:
                        pass
                try:
                    app.call_from_thread(_do_key)
                except Exception:
                    pass

            api_port = args.port + 1  # 8445 by default
            start_api_server(_ApiFrontend(), port=api_port, host=args.host,
                           highlight_callback=_on_highlight,
                           key_callback=_on_key)
        except Exception as e:
            print(f"  Frontend API: failed to start — {e}", flush=True)

    # Run textual app in main thread (needs signal handlers)
    app.run()


if __name__ == "__main__":
    main()
