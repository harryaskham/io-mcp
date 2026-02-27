#!/usr/bin/env bash
# PreToolUse hook: Report agent tool calls to io-mcp activity feed.
# Fire-and-forget HTTP POST — no latency added to tool calls.
# Runs alongside nudge-speak.sh (which handles speech reminders).

set -euo pipefail

# Read hook input
INPUT=$(cat)
TOOL=$(echo "$INPUT" | jq -r '.tool_name // ""')
SESSION_ID=$(echo "$INPUT" | jq -r '.session_id // ""')

# Skip io-mcp's own tools (already logged by the dispatch)
case "$TOOL" in
  mcp__io-mcp__*|mcp__plugin_io-mcp_io-mcp__*)
    exit 0
    ;;
esac

# Extract detail based on tool type
DETAIL=""
KIND="tool"
case "$TOOL" in
  Bash)
    DETAIL=$(echo "$INPUT" | jq -r '.tool_input.command // ""' | head -c 80)
    ;;
  Read)
    DETAIL=$(echo "$INPUT" | jq -r '.tool_input.file_path // ""' | sed "s|$HOME|~|" | tail -c 60)
    ;;
  Write)
    DETAIL=$(echo "$INPUT" | jq -r '.tool_input.file_path // ""' | sed "s|$HOME|~|" | tail -c 60)
    KIND="tool"
    ;;
  Edit)
    DETAIL=$(echo "$INPUT" | jq -r '.tool_input.file_path // ""' | sed "s|$HOME|~|" | tail -c 60)
    ;;
  Glob)
    DETAIL=$(echo "$INPUT" | jq -r '.tool_input.pattern // ""' | head -c 60)
    ;;
  Grep)
    DETAIL=$(echo "$INPUT" | jq -r '.tool_input.pattern // ""' | head -c 60)
    ;;
  WebSearch)
    DETAIL=$(echo "$INPUT" | jq -r '.tool_input.query // ""' | head -c 60)
    ;;
  WebFetch)
    DETAIL=$(echo "$INPUT" | jq -r '.tool_input.url // ""' | head -c 60)
    ;;
  Task)
    DETAIL=$(echo "$INPUT" | jq -r '.tool_input.description // .tool_input.prompt // ""' | head -c 60)
    ;;
  *)
    DETAIL=$(echo "$TOOL" | head -c 40)
    ;;
esac

# Fire-and-forget POST to backend (background, ignore errors)
# Port 8446 is the backend's /handle-mcp port
curl -s -X POST "http://localhost:8446/report-activity" \
  -H "Content-Type: application/json" \
  -d "$(jq -n --arg sid "$SESSION_ID" --arg tool "$TOOL" --arg detail "$DETAIL" --arg kind "$KIND" \
    '{session_id: $sid, tool: $tool, detail: $detail, kind: $kind}')" \
  >/dev/null 2>&1 &

# Don't block — exit immediately
exit 0
