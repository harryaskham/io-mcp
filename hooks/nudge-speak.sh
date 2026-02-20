#!/usr/bin/env bash
# PreToolUse hook: Remind Claude to speak() before significant tool calls.
# Checks if speak() was called recently — if not, nudges Claude.

set -euo pipefail

INPUT=$(cat)
TOOL=$(echo "$INPUT" | jq -r '.tool_name // ""')

# Only nudge for significant tools (not for speak/present_choices themselves)
case "$TOOL" in
  mcp__io-mcp__speak|mcp__io-mcp__present_choices|Read|Glob|Grep)
    exit 0
    ;;
esac

# We can't easily track "time since last speak" in a stateless hook,
# but we can remind Claude in the reason field.
# Exit 0 = allow, just add a gentle nudge via stdout
jq -n '{
  decision: "allow",
  reason: "Reminder: call speak() to narrate what you are about to do. The user is listening with earphones."
}'
