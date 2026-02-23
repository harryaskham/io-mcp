#!/usr/bin/env bash
# Stop hook: Block Claude from stopping unless present_choices() was called.
# If MCP is down, instructs agent to use fallback TTS via PulseAudio.

set -euo pipefail

INPUT=$(cat)

# Prevent infinite loop: if we already blocked once, allow stop
ACTIVE=$(echo "$INPUT" | jq -r '.stop_hook_active // false')
if [ "$ACTIVE" = "true" ]; then
  exit 0
fi

LAST_MSG=$(echo "$INPUT" | jq -r '.last_assistant_message // ""')

# Skip trivial/short messages
MSG_LEN=${#LAST_MSG}
if [ "$MSG_LEN" -lt 50 ]; then
  exit 0
fi

# Check if present_choices was called
if echo "$LAST_MSG" | grep -qiE 'present_choices|mcp__io-mcp__present_choices|mcp__plugin_io-mcp'; then
  exit 0
fi

# Check if fallback TTS was used (PULSE_SERVER + tts pipe)
if echo "$LAST_MSG" | grep -qiE 'PULSE_SERVER.*tts.*paplay'; then
  exit 0
fi

# Check if speak was called (at minimum)
HAS_SPEAK=false
if echo "$LAST_MSG" | grep -qiE 'speak\(|mcp__io-mcp__speak|mcp__plugin_io-mcp_io-mcp__speak'; then
  HAS_SPEAK=true
fi

FALLBACK_HINT="If io-mcp MCP tools are unavailable (connection errors), use fallback TTS instead: (PULSE_SERVER=100.67.137.9 tts \"your message\" --model gpt-4o-mini-tts --voice sage --speed 1.3 --stdout --response-format wav | PULSE_SERVER=100.67.137.9 paplay) &"

if [ "$HAS_SPEAK" = "true" ]; then
  jq -n --arg hint "$FALLBACK_HINT" '{
    decision: "block",
    reason: ("You narrated your work (good!) but forgot to call present_choices(). The user navigates via scroll wheel — they need choices to tell you what to do next. Call present_choices() with a preamble summarizing what you did and 3-5 next-step options. " + $hint)
  }'
else
  jq -n --arg hint "$FALLBACK_HINT" '{
    decision: "block",
    reason: ("CRITICAL: The user has a scroll wheel and earphones only — no keyboard, screen is off. You MUST: 1) Call speak() to briefly summarize what you did, then 2) Call present_choices() with next-step options. The user literally cannot interact with you otherwise. " + $hint)
  }'
fi
