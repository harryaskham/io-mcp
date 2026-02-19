"""
io-mcp — MCP server for agent I/O via scroll-wheel and TTS.

Exposes two MCP tools via SSE transport on port 8444:

  present_choices(preamble, choices)
      Show choices in the TUI, block until user scrolls and selects.
      Returns JSON: {"selected": "label", "summary": "..."}.

  speak(text)
      Non-blocking TTS narration through earphones. Returns immediately.

The TUI runs in a background thread reading from /dev/tty. The MCP
server uses SSE (HTTP) transport so stdin/stdout are not contended.

Usage:
    cd ~/cosmos/projects/io-mcp && uv run io-mcp
    # or: uv run io-mcp --local   (use espeak-ng instead of gpt-4o-mini-tts)
    # or: uv run io-mcp --port 9000
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal
import sys

from mcp.server.fastmcp import FastMCP

from .tui import TUI

log = logging.getLogger("io_mcp")

# Global TUI instance — started before MCP server, shared by tool handlers.
_tui: TUI | None = None


def _create_mcp(host: str = "0.0.0.0", port: int = 8444) -> FastMCP:
    """Create and configure the FastMCP server with tools."""

    server = FastMCP("io-mcp", host=host, port=port)

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
        if _tui is None:
            return json.dumps({"selected": "error", "summary": "TUI not initialized"})

        if not choices:
            return json.dumps({"selected": "error", "summary": "No choices provided"})

        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None, _tui.present_choices, preamble, choices
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
        if _tui is None:
            return "TUI not initialized"

        _tui.speak(text)
        preview = text[:100] + ("..." if len(text) > 100 else "")
        return f"Spoke: {preview}"

    return server


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
    args = parser.parse_args()

    global _tui
    _tui = TUI(local_tts=args.local)

    mcp = _create_mcp(host=args.host, port=args.port)

    def _shutdown(sig, frame):
        if _tui:
            _tui.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    # Start TUI (alt screen, input capture)
    _tui.start()

    try:
        mcp.run(transport="sse")
    finally:
        _tui.stop()


if __name__ == "__main__":
    main()
