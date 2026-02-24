"""CLI tool for sending speech and choices to io-mcp TUI.

The reverse of io-mcp-msg: instead of queuing messages for agents,
this sends output TO the user via the TUI — speak text aloud, present
choices, and get the user's selection back.

Usage:
    io-mcp-send speak "Build complete"                  # speak text (blocking)
    io-mcp-send speak-async "Working on it"             # speak without blocking
    io-mcp-send choices "What next?" "Deploy" "Rollback" "Skip"  # present choices
    io-mcp-send inbox                                   # check for queued messages
    echo "Hello" | io-mcp-send speak                    # pipe from stdin

Options:
    --host HOST       io-mcp host (default: 127.0.0.1)
    --port PORT       Backend port (default: 8446)
    -s, --session ID  Session ID (default: cli-sender)

Uses the backend REST endpoints on port 8446. Sessions auto-create
on first use — no registration needed.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
import urllib.error


def _post(base: str, path: str, body: dict, timeout: int = 300) -> str:
    """POST to the backend REST API. Returns raw response text."""
    url = f"{base}{path}"
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode()
    except urllib.error.HTTPError as e:
        body_text = e.read().decode()
        print(f"Error: {e.code} {body_text}", file=sys.stderr)
        sys.exit(1)
    except urllib.error.URLError as e:
        print(f"Error: cannot connect to io-mcp at {base}", file=sys.stderr)
        print(f"  Is io-mcp running? Start it with: io-mcp", file=sys.stderr)
        sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="io-mcp-send",
        description="Send speech and choices to the io-mcp TUI",
    )
    parser.add_argument(
        "--host", default="127.0.0.1",
        help="io-mcp host (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--port", type=int, default=8446,
        help="Backend port (default: 8446)",
    )
    parser.add_argument(
        "-s", "--session", default="cli-sender",
        help="Session ID (default: cli-sender)",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    # speak (blocking)
    sp = sub.add_parser("speak", help="Speak text aloud (blocks until done)")
    sp.add_argument("text", nargs="*", help="Text to speak")

    # speak-async (non-blocking)
    sa = sub.add_parser("speak-async", help="Speak text without blocking")
    sa.add_argument("text", nargs="*", help="Text to speak")

    # choices (blocking — waits for selection)
    ch = sub.add_parser("choices", help="Present choices and wait for selection")
    ch.add_argument("preamble", help="Preamble spoken before choices")
    ch.add_argument("options", nargs="+", help="Choice labels")

    # inbox
    sub.add_parser("inbox", help="Check for queued user messages")

    args = parser.parse_args()
    base = f"http://{args.host}:{args.port}"

    if args.command in ("speak", "speak-async"):
        text = " ".join(args.text) if args.text else ""
        if not text and not sys.stdin.isatty():
            text = sys.stdin.read().strip()
        if not text:
            print("Error: no text provided", file=sys.stderr)
            sys.exit(1)

        endpoint = "/speak" if args.command == "speak" else "/speak-async"
        result = _post(base, endpoint, {
            "text": text,
            "session_id": args.session,
        })
        if args.command == "speak":
            print("✓ Spoken")
        else:
            print("✓ Speaking (async)")

    elif args.command == "choices":
        choices = [{"label": opt, "summary": ""} for opt in args.options]
        result = _post(base, "/choices", {
            "preamble": args.preamble,
            "choices": choices,
            "session_id": args.session,
        })
        # Parse the selection from the result
        try:
            data = json.loads(result)
            selected = data.get("selected", "")
            print(selected)
        except json.JSONDecodeError:
            print(result)

    elif args.command == "inbox":
        result = _post(base, "/inbox", {
            "session_id": args.session,
        })
        try:
            data = json.loads(result)
            messages = data.get("messages", [])
            if messages:
                for msg in messages:
                    print(msg)
            else:
                print("(no messages)")
        except json.JSONDecodeError:
            print(result)


if __name__ == "__main__":
    main()
