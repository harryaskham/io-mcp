---
name: restart-io-mcp
description: Kill and restart the io-mcp TUI and/or proxy server, then reconnect MCP if needed. Use when io-mcp is stuck, crashed, or needs a fresh start.
---

# restart-io-mcp — Restart io-mcp Services

Kills the io-mcp TUI backend (which auto-restarts via its while loop) and optionally
the MCP proxy server. If the proxy was killed, reconnects the MCP connection.

## Usage

Invoke `/restart-io-mcp` when:
- The io-mcp TUI is stuck or unresponsive
- You need to pick up code changes in io-mcp
- The MCP connection is broken
- Speech/TTS has stopped working

## Steps

### 1. Determine what's running and where

```bash
# Check health of all services
curl -sf http://localhost:8444/health 2>/dev/null && echo "Proxy: OK" || echo "Proxy: DOWN"
curl -sf http://localhost:8446/health 2>/dev/null && echo "Backend: OK" || echo "Backend: DOWN"
tmux has-session -t io-mcp-tui 2>/dev/null && echo "TUI session: exists" || echo "TUI session: missing"
```

### 2. Kill the TUI backend

The TUI runs in the `io-mcp-tui` tmux session inside a `while true` loop, so it
will **auto-restart** after being killed. Just send Ctrl+C:

```bash
# Send Ctrl+C — the while loop will restart io-mcp automatically
tmux send-keys -t io-mcp-tui C-c
sleep 2
# Second Ctrl+C if needed (sometimes the first one is caught by a subprocess)
tmux send-keys -t io-mcp-tui C-c
```

For **remote** io-mcp (running on another machine via SSH):

```bash
ssh HOSTNAME "tmux send-keys -t io-mcp-tui C-c"
sleep 2
ssh HOSTNAME "tmux send-keys -t io-mcp-tui C-c"
```

The TUI will auto-restart within a few seconds. Wait for it:

```bash
for i in $(seq 1 30); do
  curl -sf http://localhost:8446/health 2>/dev/null && break
  sleep 1
done
echo "Backend is back up"
```

**No MCP reconnection needed** — the proxy survives TUI restarts and agent
connections are preserved.

### 3. Kill the proxy (only if needed)

Only kill the proxy if:
- Proxy code was changed and needs reloading
- The proxy itself is stuck/unhealthy
- MCP connections are broken at the proxy level

**Warning: killing the proxy disconnects ALL agents. You WILL need to reconnect MCP.**

```bash
# Kill proxy by PID file
kill $(cat /tmp/io-mcp-server.pid 2>/dev/null) 2>/dev/null
sleep 2
```

The proxy auto-starts with the next TUI launch (which happens via the while loop).
Wait for it:

```bash
for i in $(seq 1 15); do
  curl -sf http://localhost:8444/health 2>/dev/null && break
  sleep 1
done
echo "Proxy is back up"
```

### 4. Reconnect MCP (only if proxy was killed)

If you killed the proxy in step 3, reconnect your MCP client.

**Method A — Using tmux-cli to send /mcp to yourself:**

```bash
# Get your own pane ID
MYPANE=$(tmux display-message -p '#{pane_id}')

# Send the /mcp reconnect sequence
tmux send-keys -t "$MYPANE" '/mcp' Enter
sleep 3
tmux send-keys -t "$MYPANE" Enter    # select io-mcp
sleep 2
tmux send-keys -t "$MYPANE" Down Enter  # Reconnect
sleep 4
tmux send-keys -t "$MYPANE" Escape Escape
```

**Method B — Manual:**

1. Run `/mcp` in your Claude Code session
2. Select the io-mcp server
3. Choose "Reconnect"

### 5. Verify

```bash
curl -sf http://localhost:8444/health && echo "Proxy OK"
curl -sf http://localhost:8446/health && echo "Backend OK"
curl -sf http://localhost:8445/api/health && echo "API OK"
```

## Quick Reference

| Scenario | Kill TUI? | Kill proxy? | Reconnect MCP? |
|----------|-----------|-------------|-----------------|
| TUI stuck | Yes | No | No |
| TUI code changes | Yes | No | No |
| Proxy code changes | Yes | Yes | **Yes** |
| MCP broken | Yes | Maybe | If proxy killed |
| Everything broken | Yes | Yes | **Yes** |

## Notes

- The TUI runs in a `while true` loop — killing it triggers an automatic restart.
- The proxy survives TUI restarts. Only kill it when absolutely necessary.
- When the proxy restarts, ALL connected agents lose their MCP connection.
- Use `io-mcp --restart` flag for a clean start that kills stale processes first.
