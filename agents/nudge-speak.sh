#!/usr/bin/env bash
# PreToolUse hook: Remind Claude to speak_async() if it's been too long since
# it last narrated. Uses a timestamp file updated by the backend on each
# speak/speak_async/speak_urgent/present_choices call.
#
# Always allows the tool call — never denies. Only adds a reminder reason
# when the agent has been silent for >60 seconds.

set -euo pipefail

INPUT=$(cat)
TOOL=$(echo "$INPUT" | jq -r '.tool_name // ""')

# Don't nudge for io-mcp tools (speech tools update the timestamp themselves),
# read-only tools, subagent tools, or search tools
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

# Identify the agent session — prefer TMUX_PANE (always available in agent env),
# fall back to session_id from hook input, then "default"
AGENT_ID="${TMUX_PANE:-}"
if [ -z "$AGENT_ID" ]; then
  AGENT_ID=$(echo "$INPUT" | jq -r '.session_id // "default"')
fi
# Sanitize for filename (strip %)
AGENT_ID=$(echo "$AGENT_ID" | tr -d '%')

TIMESTAMP_FILE="/tmp/io-mcp-last-speech-${AGENT_ID}"
NUDGE_FILE="/tmp/io-mcp-last-nudge-${AGENT_ID}"
THRESHOLD=60   # seconds before nudging
COOLDOWN=120   # minimum seconds between nudges (avoid spam)

# If no timestamp file exists, the agent hasn't spoken yet — but don't nudge
# on the very first tool calls (give them a chance to start up). Create the
# file and allow silently.
if [ ! -f "$TIMESTAMP_FILE" ]; then
  date +%s > "$TIMESTAMP_FILE"
  exit 0
fi

# Read the last speech timestamp
LAST_SPEECH=$(cat "$TIMESTAMP_FILE" 2>/dev/null || echo "0")
NOW=$(date +%s)
ELAPSED=$((NOW - LAST_SPEECH))

# Not long enough — allow silently
if [ "$ELAPSED" -le "$THRESHOLD" ]; then
  exit 0
fi

# Check cooldown — don't nudge too frequently
if [ -f "$NUDGE_FILE" ]; then
  LAST_NUDGE=$(cat "$NUDGE_FILE" 2>/dev/null || echo "0")
  NUDGE_ELAPSED=$((NOW - LAST_NUDGE))
  if [ "$NUDGE_ELAPSED" -le "$COOLDOWN" ]; then
    exit 0
  fi
fi

# Record this nudge time
echo "$NOW" > "$NUDGE_FILE"

# Allow the tool but remind about narration
jq -n --argjson elapsed "$ELAPSED" '{
  decision: "allow",
  reason: ("[REMINDER: It\u0027s been " + ($elapsed | tostring) + " seconds since you last called speak_async(). The user is listening through earphones and can\u0027t see the screen — call speak_async() to narrate what you\u0027re doing!]")
}'
