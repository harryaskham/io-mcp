#!/usr/bin/env bash
# Start hook: Register agent environment with io-mcp.
#
# Gathers tmux pane, session, IP, Tailscale hostname, and writes to
# a registration file that the proxy reads on register_session().
# This removes the burden from agents to fill out their own metadata.

set -euo pipefail

REG_DIR="/tmp/io-mcp-registrations"
mkdir -p "$REG_DIR"

# Gather environment data
PANE_ID="${TMUX_PANE:-}"
if [ -z "$PANE_ID" ]; then
  # Not in tmux — skip registration
  exit 0
fi

# tmux session name
TMUX_SESS=""
if command -v tmux &>/dev/null; then
  TMUX_SESS=$(tmux display-message -p '#{session_name}' 2>/dev/null || true)
fi

# IPv4 address — prefer non-loopback
IPV4=""
if command -v ip &>/dev/null; then
  IPV4=$(ip -4 addr show | grep 'inet ' | grep -v '127.0.0.1' | awk '{print $2}' | cut -d/ -f1 | head -1 || true)
elif command -v ifconfig &>/dev/null; then
  IPV4=$(ifconfig | grep 'inet ' | grep -v '127.0.0.1' | awk '{print $2}' | head -1 || true)
fi

# Tailscale hostname
TS_HOSTNAME=""
if command -v tailscale &>/dev/null; then
  TS_HOSTNAME=$(tailscale status --json 2>/dev/null | jq -r '.Self.DNSName // ""' 2>/dev/null | sed 's/\.$//' || true)
fi

# System hostname
SYS_HOSTNAME=$(hostname 2>/dev/null || echo "unknown")

# Current working directory
CWD=$(pwd 2>/dev/null || echo "")

# Write registration file keyed by pane ID (% stripped for filename safety)
PANE_KEY=$(echo "$PANE_ID" | tr -d '%')
jq -n \
  --arg pane_id "$PANE_ID" \
  --arg tmux_session "$TMUX_SESS" \
  --arg ipv4 "$IPV4" \
  --arg tailscale_hostname "$TS_HOSTNAME" \
  --arg hostname "$SYS_HOSTNAME" \
  --arg cwd "$CWD" \
  --arg timestamp "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  '{
    tmux_pane: $pane_id,
    tmux_session: $tmux_session,
    ipv4: $ipv4,
    tailscale_hostname: $tailscale_hostname,
    hostname: $hostname,
    cwd: $cwd,
    timestamp: $timestamp
  }' > "$REG_DIR/pane-$PANE_KEY.json"
