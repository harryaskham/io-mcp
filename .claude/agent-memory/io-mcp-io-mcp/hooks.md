# io-mcp Agent Hooks

All hooks for io-mcp agents are defined in the agent frontmatter in `agents/io-mcp.md` (and `agents/io-mcp-admin.md`). Hook scripts live in `agents/`.

## Current Hooks

| Hook | Script | Purpose |
|------|--------|---------|
| Start | `start-register.sh` | Auto-register session with io-mcp on agent start |
| Stop | `enforce-choices.sh` | Block agent from stopping without presenting choices |
| PreToolUse (Bash\|Edit\|Write\|NotebookEdit\|Task) | `nudge-speak.sh` | Remind agent to narrate via speak_async() |
| PreToolUse (Bash\|Edit\|Write\|Read\|Glob\|Grep\|...) | `report-activity.sh` | Report tool calls to io-mcp activity feed |
| PreToolUse (Bash) | `enforce-background.sh` | Deny long-running commands that aren't backgrounded |

## Hook Capabilities

- PreToolUse hooks can **deny** with a reason message (agent sees the denial and retries)
- PreToolUse hooks can **modify** tool inputs via `updatedInput` in the JSON response
- PreToolUse hooks receive JSON on stdin with `tool_name`, `tool_input`, `session_id`
- Exit code 0 = success, exit code 2 = blocking error

## Adding New Hooks

1. Create the script in `agents/` directory
2. Make it executable (`chmod +x`)
3. Add the hook entry to the frontmatter in `agents/io-mcp.md`
4. Use `matcher` to target specific tools (e.g., `"Bash"` for Bash-only hooks)

## Hook Ideas for Future

- PostToolUse hook to detect test failures and auto-narrate them
- PreToolUse hook to enforce speak_async() frequency (if last speech was >60s ago, deny non-speech tools)
- Hook to detect when agent is about to commit and remind about running tests first
