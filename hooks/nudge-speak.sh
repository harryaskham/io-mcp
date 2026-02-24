#!/usr/bin/env bash
# PreToolUse hook: Remind Claude to speak_async() before significant tool calls.
# Only nudges for tools that do real work (not speech or choice tools).

set -euo pipefail

INPUT=$(cat)
TOOL=$(echo "$INPUT" | jq -r '.tool_name // ""')

# Don't nudge for io-mcp tools (both prefixed forms), read-only tools,
# subagent tools, or search tools
case "$TOOL" in
  mcp__io-mcp__*|mcp__plugin_io-mcp_io-mcp__*)
    exit 0
    ;;
  Read|Glob|Grep|WebSearch|WebFetch)
    exit 0
    ;;
  Task|TaskOutput|TaskStop|TaskCreate|TaskUpdate|TaskGet|TaskList)
    exit 0
    ;;
  EnterPlanMode|ExitPlanMode|AskUserQuestion|Skill)
    exit 0
    ;;
esac

# Allow the tool but remind about narration
jq -n '{
  decision: "allow",
  reason: "Remember: call speak_async() to narrate what you are doing. The user is listening with earphones and cannot see the screen."
}'
