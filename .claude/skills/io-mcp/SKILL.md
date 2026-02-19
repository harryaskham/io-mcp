---
name: io-mcp
description: Hands-free Claude Code via scroll wheel and earphones. Narrate progress and present multi-choice decision points.
---

# io-mcp — Hands-Free Interaction Mode

You are working with a user who controls Claude Code using only a **scroll wheel** (smart ring) and **earphones**. They cannot type. All interaction happens through two MCP tools: `speak` and `present_choices`.

## How It Works

The user has a TUI running in another pane that shows choices. They scroll through options hearing each label read aloud, then dwell for 5 seconds (or press Enter) to select.

## Rules

### 1. Narrate Everything via `speak()`

Call `speak()` frequently — every 20-30 seconds while working — with short verbal status updates. The user is listening with earphones and has the screen off.

**Good narration examples:**
- `speak("Reading the test file to understand the failures")`
- `speak("Found the bug — a missing null check on line 42")`
- `speak("Writing the fix now")`
- `speak("Running tests. Three passed, one still failing.")`
- `speak("Done. All tests pass. Ready for next steps.")`

**Bad narration:**
- Too long: `speak("I am now going to read through the entire codebase to understand the architecture and then I will identify the relevant files and make changes")` — break this into multiple short updates.
- Too infrequent: Working for 2 minutes without any `speak()` call.

### 2. Present Choices at Decision Points via `present_choices()`

After completing a unit of work (or when you need input), call `present_choices()` with:

- **preamble**: 1 sentence summarizing what happened and what's next. This is spoken aloud.
- **choices**: 3-7 options with short labels (2-5 words, read aloud on scroll) and summaries.

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
    {"label": "Run full suite", "summary": "Run the complete test suite, not just the affected tests"},
    {"label": "Show the diff", "summary": "Review exactly what was changed before committing"},
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
3. You work on the task, calling `speak()` for status updates
4. At decision points or when done, call `present_choices()`
5. Wait for the result — the user's selection comes back as the tool response
6. Continue based on their choice, narrating as you go
7. Repeat steps 3-6

### 5. Starting a Session

When `/io-mcp` is invoked:

1. `speak("Starting ringchat session. Tell me what to work on.")`
2. `present_choices(preamble="What would you like me to work on?", choices=[...])`
   - Include common starting points relevant to the current project
   - Include "Describe a task" for open-ended requests (you'll need to ask follow-up choices to narrow down)
