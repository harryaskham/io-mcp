#!/usr/bin/env bash
# PreToolUse hook: Deny long-running Bash commands that aren't backgrounded.
# The agent will see the denial reason and retry with run_in_background=true,
# keeping the speech channel active during long operations.

set -euo pipefail

INPUT=$(cat)
TOOL=$(echo "$INPUT" | jq -r '.tool_name // ""')

# Only applies to Bash tool
if [ "$TOOL" != "Bash" ]; then
  exit 0
fi

# Check if already backgrounded
RUN_IN_BG=$(echo "$INPUT" | jq -r '.tool_input.run_in_background // false')
if [ "$RUN_IN_BG" = "true" ]; then
  exit 0
fi

# Extract the command
CMD=$(echo "$INPUT" | jq -r '.tool_input.command // ""')

# Pattern-match known long-running commands
IS_LONG_RUNNING=false

case "$CMD" in
  # Test runners
  *pytest*|*"uv run pytest"*|*"npm test"*|*"npm run test"*|*"cargo test"*|*"go test"*|*"make test"*|*"just test"*)
    IS_LONG_RUNNING=true
    ;;
  # Build commands
  *"cargo build"*|*"npm run build"*|*"make build"*|*"nix build"*|*"gradle"*|*"mvn "*|*"just build"*)
    IS_LONG_RUNNING=true
    ;;
  # Package install
  *"npm install"*|*"pip install"*|*"uv sync"*|*"nix develop"*|*"yarn install"*|*"pnpm install"*)
    IS_LONG_RUNNING=true
    ;;
esac

if [ "$IS_LONG_RUNNING" = "true" ]; then
  jq -n '{
    decision: "deny",
    reason: "This command looks like it will take a while. Re-run it with run_in_background: true so you can keep narrating via speak_async() while it runs. The user is listening through earphones and silence feels broken."
  }'
else
  # Allow short commands through
  exit 0
fi
