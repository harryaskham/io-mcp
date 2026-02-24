---
name: io-mcp-msg
description: Queue messages for io-mcp agent sessions. Use to send instructions, context, or nudges to agents that are connected to io-mcp.
---

# io-mcp-msg

Queue messages for running io-mcp agent sessions. Messages appear in the
agent's next MCP tool response via the `pending_messages` queue.

## Commands

### Send to all sessions
```bash
io-mcp-msg "Please check the test failures in auth.py"
```

### Send to the focused/active session
```bash
io-mcp-msg --active "Look at this file"
```

### Send to a specific session
```bash
io-mcp-msg -s SESSION_ID "Switch to the feature branch"
```

### List active sessions
```bash
io-mcp-msg --list
```

### Check io-mcp health
```bash
io-mcp-msg --health
```

### Pipe from stdin
```bash
echo "Build failed, check logs" | io-mcp-msg
```

## Options

- `--host HOST` — io-mcp host (default: 127.0.0.1)
- `--port PORT` — Frontend API port (default: 8445)
- `-s, --session ID` — Target specific session ID
- `--active` — Send to focused session only
- `--list` — List active sessions and exit
- `--health` — Check io-mcp health and exit

## Notes

- Messages are queued in-memory and delivered on the agent's next tool call
- Uses the Frontend API on port 8445 (not the backend on 8446)
- Messages are lost on backend restart (no persistence)
- Use `io-mcp-send` instead if you want to speak to the USER (TTS) or present choices
