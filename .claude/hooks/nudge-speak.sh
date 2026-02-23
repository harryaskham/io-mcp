#!/usr/bin/env bash
# PreToolUse hook: Remind Claude to speak_async() before significant tool calls.
# Only nudges for tools that do real work (not speech or choice tools).

set -euo pipefail

INPUT=$(cat)
TOOL=$(echo "$INPUT" | jq -r '.tool_name // ""')

# Don't nudge for io-mcp tools, read-only tools, or search tools
case "$TOOL" in
  mcp__io-mcp__speak|mcp__io-mcp__speak_async|mcp__io-mcp__speak_urgent)
    exit 0
    ;;
  mcp__io-mcp__present_choices|mcp__io-mcp__present_multi_select)
    exit 0
    ;;
  mcp__io-mcp__set_*|mcp__io-mcp__get_*|mcp__io-mcp__rename_*)
    exit 0
    ;;
  Read|Glob|Grep|WebSearch|WebFetch)
    exit 0
    ;;
esac

# Allow the tool but remind about narration
jq -n '{
  decision: "allow",
  reason: "Remember: call speak_async() to narrate what you are doing. The user is listening with earphones and cannot see the screen."
}'
