---
name: restart-io-mcp
description: Kill and restart the io-mcp TUI and/or proxy server, then reconnect MCP if needed. Use when io-mcp is stuck, crashed, or needs a fresh start.
---

# restart-io-mcp — Restart io-mcp Services

Kills and restarts the io-mcp TUI backend and optionally the MCP proxy server.
After restarting, reconnects the MCP connection if the proxy was killed.

## Usage

Invoke `/restart-io-mcp` when:
- The io-mcp TUI is stuck or unresponsive
- You need to pick up code changes in io-mcp
- The MCP connection is broken
- Speech/TTS has stopped working

## Steps

### 1. Determine what's running and where

```bash
# Check if io-mcp-tui tmux session exists
tmux has-session -t io-mcp-tui 2>/dev/null && echo "TUI session exists" || echo "No TUI session"

# Check if proxy is running
curl -sf http://localhost:8444/health 2>/dev/null && echo "Proxy healthy" || echo "Proxy down"

# Check if backend is running
curl -sf http://localhost:8446/health 2>/dev/null && echo "Backend healthy" || echo "Backend down"
```

### 2. Kill the TUI backend

The TUI runs in the `io-mcp-tui` tmux session. Kill it by sending Ctrl+C:

```bash
# Send Ctrl+C to stop the TUI gracefully
tmux send-keys -t io-mcp-tui C-c
sleep 2

# If still running, force kill
tmux send-keys -t io-mcp-tui C-c
sleep 1
```

For **remote** io-mcp (running on another machine via SSH):

```bash
# Replace HOSTNAME with the io-mcp host (e.g. phone's Tailscale hostname)
ssh HOSTNAME "tmux send-keys -t io-mcp-tui C-c"
sleep 2
```

### 3. Optionally kill the proxy

Only kill the proxy if it's unhealthy or you need to pick up proxy code changes.
**Warning: killing the proxy breaks ALL agent MCP connections.**

```bash
# Kill proxy by PID file
kill $(cat /tmp/io-mcp-server.pid 2>/dev/null) 2>/dev/null
# Or: io-mcp restart-proxy (if the CLI is available)
```

### 4. Restart io-mcp

```bash
# Restart in the existing tmux session
tmux send-keys -t io-mcp-tui "uv run io-mcp" Enter

# Or for remote:
# ssh HOSTNAME "tmux send-keys -t io-mcp-tui 'uv run io-mcp' Enter"
```

Wait for it to come up:

```bash
# Wait for backend to be healthy (up to 30 seconds)
for i in $(seq 1 30); do
  curl -sf http://localhost:8446/health 2>/dev/null && break
  sleep 1
done
```

### 5. Reconnect MCP (if proxy was killed)

If you killed the proxy in step 3, you need to reconnect your MCP client.
The proxy auto-starts with the backend, so just wait for it:

```bash
# Wait for proxy to be healthy
for i in $(seq 1 15); do
  curl -sf http://localhost:8444/health 2>/dev/null && break
  sleep 1
done
```

Then use `/mcp` to reconnect:
1. Run `/mcp` in your Claude Code session
2. Select the io-mcp server
3. Choose "Reconnect"

If using tmux-cli to reconnect yourself:

```bash
# Get your own tmux pane ID
MYPANE=$(tmux display-message -p '#{pane_id}')

# Send /mcp command to yourself
tmux send-keys -t "$MYPANE" '/mcp' Enter
sleep 3
tmux send-keys -t "$MYPANE" Enter  # select io-mcp
sleep 2
tmux send-keys -t "$MYPANE" Down Enter  # Reconnect
sleep 4
tmux send-keys -t "$MYPANE" Escape Escape  # dismiss
```

### 6. Verify

After restart, verify everything is working:

```bash
# Check all services
io-mcp status 2>/dev/null || echo "io-mcp CLI not available"
curl -sf http://localhost:8444/health && echo "Proxy OK"
curl -sf http://localhost:8446/health && echo "Backend OK"
curl -sf http://localhost:8445/api/health && echo "API OK"
```

## Quick Reference

| Scenario | Kill proxy? | Reconnect MCP? |
|----------|-------------|-----------------|
| TUI stuck/crashed | No | No |
| Code changes in TUI | No | No |
| Code changes in proxy | Yes | Yes |
| MCP connection broken | Maybe | Yes |
| Everything broken | Yes | Yes |

## Notes

- The proxy is designed to survive TUI restarts. Only kill it when necessary.
- When the proxy restarts, ALL connected agents lose their MCP connection.
- The TUI has a built-in restart loop — it will auto-restart if it crashes.
- Use `io-mcp --restart` flag to force-kill all stale processes before starting.
