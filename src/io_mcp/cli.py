"""CLI tool for sending messages to running io-mcp sessions.

Usage:
    io-mcp-msg "check this file"                    # broadcast to all sessions
    io-mcp-msg -s SESSION_ID "look at auth.py"      # send to specific session
    io-mcp-msg --active "hey, you there?"            # send to focused session
    io-mcp-msg --list                                # list active sessions
    io-mcp-msg --host 192.168.1.5 "remote message"  # send to remote io-mcp

Works by hitting the Frontend API (port 8445 by default). The message
will be picked up by the agent on its next MCP tool call via the
pending_messages queue.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
import urllib.error


def _api_get(base: str, path: str) -> dict:
    """GET request to the Frontend API."""
    url = f"{base}{path}"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read())
    except urllib.error.URLError as e:
        print(f"Error: cannot connect to io-mcp at {base}", file=sys.stderr)
        print(f"  {e}", file=sys.stderr)
        sys.exit(1)


def _api_post(base: str, path: str, body: dict) -> dict:
    """POST request to the Frontend API."""
    url = f"{base}{path}"
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body_text = e.read().decode()
        try:
            err = json.loads(body_text)
            print(f"Error: {err.get('error', body_text)}", file=sys.stderr)
        except json.JSONDecodeError:
            print(f"Error: {e.code} {body_text}", file=sys.stderr)
        sys.exit(1)
    except urllib.error.URLError as e:
        print(f"Error: cannot connect to io-mcp at {base}", file=sys.stderr)
        print(f"  {e}", file=sys.stderr)
        sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="io-mcp-msg",
        description="Send messages to running io-mcp agent sessions",
    )
    parser.add_argument(
        "message", nargs="*",
        help="Message text to send (all args joined with spaces)",
    )
    parser.add_argument(
        "--host", default="127.0.0.1",
        help="io-mcp host (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--port", type=int, default=8445,
        help="Frontend API port (default: 8445)",
    )
    parser.add_argument(
        "-s", "--session", default=None,
        help="Target specific session ID",
    )
    parser.add_argument(
        "--active", action="store_true",
        help="Send to the focused/active session only",
    )
    parser.add_argument(
        "--list", action="store_true", dest="list_sessions",
        help="List active sessions and exit",
    )
    parser.add_argument(
        "--health", action="store_true",
        help="Check io-mcp health and exit",
    )
    args = parser.parse_args()

    base = f"http://{args.host}:{args.port}"

    # Health check
    if args.health:
        result = _api_get(base, "/api/health")
        print(json.dumps(result, indent=2))
        return

    # List sessions
    if args.list_sessions:
        result = _api_get(base, "/api/sessions")
        sessions = result.get("sessions", [])
        if not sessions:
            print("No active sessions")
            return
        for s in sessions:
            status = "🟢 choices" if s.get("active") else "⏳ working"
            print(f"  {s['id'][:12]}  {status}  {s.get('name', '(unnamed)')}")
        return

    # Send message
    message = " ".join(args.message) if args.message else ""
    if not message:
        # Read from stdin if no message args
        if not sys.stdin.isatty():
            message = sys.stdin.read().strip()
        if not message:
            parser.print_help()
            sys.exit(1)

    if args.session:
        # Send to specific session
        result = _api_post(base, f"/api/sessions/{args.session}/message",
                          {"text": message})
        print(f"✓ Queued to session {args.session[:12]} ({result.get('pending', '?')} pending)")
    else:
        # Broadcast
        target = "active" if args.active else "all"
        result = _api_post(base, "/api/message",
                          {"text": message, "target": target})
        count = result.get("count", 0)
        print(f"✓ Queued to {count} session{'s' if count != 1 else ''}")


if __name__ == "__main__":
    main()
