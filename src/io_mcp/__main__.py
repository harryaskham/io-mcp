"""
io-mcp — MCP server for agent I/O via scroll-wheel and TTS.

Exposes two MCP tools via SSE transport on port 8444:

  present_choices(preamble, choices)
      Show choices in the TUI, block until user scrolls and selects.
      Returns JSON: {"selected": "label", "summary": "..."}.

  speak(text)
      Non-blocking TTS narration through earphones. Returns immediately.

Textual TUI runs in the main thread (needs signal handlers).
MCP SSE server runs in a background thread via uvicorn.

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
import threading
import sys

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


def _run_mcp_server(app: IoMcpApp, host: str, port: int, append_options: list[str] | None = None) -> None:
    """Run the MCP SSE server in a background thread."""
    from mcp.server.fastmcp import FastMCP

    server = FastMCP("io-mcp", host=host, port=port)
    _append = append_options or []

    @server.tool()
    async def present_choices(
        preamble: str,
        choices: list[dict],
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

        # Append persistent options from --append-option flags
        all_choices = list(choices)
        for opt in _append:
            # Don't duplicate if Claude already included it
            if not any(c.get("label", "").lower() == opt.lower() for c in all_choices):
                all_choices.append({"label": opt, "summary": f"(persistent option)"})

        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None, app.present_choices, preamble, all_choices
        )

        return json.dumps(result)

    @server.tool()
    async def speak(text: str) -> str:
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
        app.speak(text)
        preview = text[:100] + ("..." if len(text) > 100 else "")
        return f"Spoke: {preview}"

    # Run SSE server (blocks this thread)
    server.run(transport="sse")


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
        help="SSE server port (default: 8444)"
    )
    parser.add_argument(
        "--host", default="0.0.0.0",
        help="SSE server host (default: 0.0.0.0)"
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
    args = parser.parse_args()

    tts = TTSEngine(local=args.local)

    # Write PID file so global hooks can detect io-mcp is running
    _write_pid_file()
    atexit.register(_remove_pid_file)

    # Create the textual app
    app = IoMcpApp(tts=tts, dwell_time=args.dwell, scroll_debounce=args.scroll_debounce)

    # Start MCP SSE server in background thread
    mcp_thread = threading.Thread(
        target=_run_mcp_server,
        args=(app, args.host, args.port, args.append_option),
        daemon=True,
    )
    mcp_thread.start()

    # Run textual app in main thread (needs signal handlers)
    app.run()


if __name__ == "__main__":
    main()
