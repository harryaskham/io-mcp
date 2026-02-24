---
name: io-mcp-send
description: Send speech and choices to the io-mcp TUI from any context. Use to speak to the user or present choices via the scroll-wheel interface when you're not connected as an io-mcp agent.
---

# io-mcp-send

Send speech and choices to the io-mcp TUI from any script or agent context.
This is the reverse of io-mcp-msg — instead of queuing messages for agents,
this sends output TO the user via the TUI.

## Commands

### Speak text (blocking — waits for TTS to finish)
```bash
io-mcp-send speak "Build complete, all tests passing"
```

### Speak text (non-blocking — returns immediately)
```bash
io-mcp-send speak-async "Working on deployment"
```

### Present choices and get user selection
```bash
SELECTION=$(io-mcp-send choices "What should I deploy?" "Production" "Staging" "Cancel")
echo "User selected: $SELECTION"
```
This blocks until the user scrolls and selects an option. The selected label is printed to stdout.

### Check inbox for queued user messages
```bash
io-mcp-send inbox
```

### Pipe text from stdin
```bash
echo "Hello from my script" | io-mcp-send speak
```

## Options

- `--host HOST` — io-mcp host (default: 127.0.0.1)
- `--port PORT` — Backend port (default: 8446)
- `-s, --session ID` — Session ID (default: cli-sender). Use a consistent ID to reuse the same TUI tab.

## Examples

### Shell script with user interaction
```bash
#!/bin/bash
io-mcp-send speak-async "Starting deployment checks"

if make test; then
    CHOICE=$(io-mcp-send choices "Tests passed. Deploy?" "Deploy to prod" "Deploy to staging" "Skip")
    case "$CHOICE" in
        "Deploy to prod") make deploy-prod ;;
        "Deploy to staging") make deploy-staging ;;
        *) io-mcp-send speak "Deployment skipped" ;;
    esac
else
    io-mcp-send speak "Tests failed. Check the logs."
fi
```

## Notes

- Sessions auto-create on first use — no registration needed
- Uses the backend REST API on port 8446
- The `choices` command blocks until the user makes a selection
- Multiple scripts can use different session IDs to get separate TUI tabs
