---
name: io-mcp
description: Hands-free Claude Code via scroll wheel and earphones. Narrate progress and present multi-choice decision points.
---

# io-mcp — Hands-Free Interaction Mode

You are working with a user who controls Claude Code using only a **scroll wheel** (smart ring) and **earphones**. They cannot type. All interaction happens through two MCP tools: `speak` and `present_choices`.

**⚠️ ENFORCEMENT: A Stop hook will block you from finishing if you don't call these tools. Don't fight it — just call them.**

## How It Works

The user has a TUI running in another pane that shows choices. They scroll through options hearing each label read aloud, then press Enter (or dwell) to select.

## Rules

### 1. Narrate Everything via `speak()`

Call `speak()` **before and after every significant action** — reading files, writing code, running commands, analyzing output. The user is listening with earphones and may have the screen off.

**Pattern:** speak → do work → speak → do more work → speak → present_choices

**Good narration examples:**
- `speak("Reading the test file to understand the failures")`
- `speak("Found the bug — a missing null check on line 42")`
- `speak("Writing the fix now")`
- `speak("Running tests. Three passed, one still failing.")`
- `speak("Done. All tests pass. Ready for next steps.")`

**Bad narration:**
- Too long: `speak("I am now going to read through the entire codebase...")` — break into short updates.
- Too infrequent: Working for 2+ minutes without any `speak()` call.
- Missing entirely: A Stop hook will catch this and force you to narrate.

### 2. ALWAYS End with `present_choices()`

**Every response must end with `present_choices()`.** This is non-negotiable. The user has no keyboard — `present_choices()` is their ONLY way to tell you what to do next.

After completing a unit of work (or when you need input), call `present_choices()` with:

- **preamble**: 1 sentence summarizing what happened and what's next. This is spoken aloud.
- **choices**: 3-5 options with short labels (2-5 words, read aloud on scroll) and summaries.

**Always include these meta-choices where appropriate:**
- "Continue" — keep going with the current approach
- "More detail" — explain what was done in more depth
- "Change approach" — try a different strategy
- "Stop here" — pause and wait

**Example:**
```
present_choices(
  preamble="Fixed the null check bug. All 12 tests pass now.",
  choices=[
    {"label": "Commit changes", "summary": "Stage and commit the fix with a descriptive message"},
    {"label": "Run full suite", "summary": "Run the complete test suite, not just affected tests"},
    {"label": "Show the diff", "summary": "Review what was changed before committing"},
    {"label": "Fix something else", "summary": "Move on to the next issue"},
    {"label": "Stop here", "summary": "Pause work and wait"}
  ]
)
```

### 3. Label Guidelines

Labels are read aloud via TTS on **every scroll**. They must be:
- **2-5 words** — concise enough to speak quickly
- **Distinct** — each label should sound different when spoken
- **Action-oriented** — "Run tests", "Show diff", "Commit changes"

### 4. Flow

1. User invokes `/io-mcp` with a task description
2. You `speak()` what you're about to do
3. You work on the task, calling `speak()` for status updates every 20-30 seconds
4. When done or at a decision point, call `present_choices()`
5. Wait for the result — the user's selection comes back as the tool response
6. Continue based on their choice, narrating as you go
7. Repeat steps 3-6

### 5. Starting a Session

When `/io-mcp` is invoked:

1. `speak("Starting hands-free session. Tell me what to work on.")`
2. `present_choices(preamble="What would you like me to work on?", choices=[...])`
   - Include common starting points relevant to the current project
   - Include "Describe a task" for open-ended requests

### 6. What NOT to Do

- **Never** finish a response without calling `present_choices()` — the user is stuck
- **Never** ask a question in text — the user can't read it; use `present_choices()` instead
- **Never** work for more than 30 seconds without calling `speak()`
- **Never** use `AskUserQuestion` — use `present_choices()` instead
- **Never** output long text responses — narrate via `speak()` and present options via `present_choices()`
