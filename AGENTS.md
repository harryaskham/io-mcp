# io-mcp — Agent Guide

MCP server providing hands-free Claude Code interaction via scroll wheel (smart ring) and earphones (TTS). Runs on Nix-on-Droid (Galaxy S24 Ultra, aarch64, Android 14). Native Android companion app available.

## Architecture

```
┌─────────────────┐   SSE :8444   ┌──────────────┐
│  Claude Code A  │◄────────────►│  MCP Server   │
│  (agent)        │               │  (server.py)  │
└─────────────────┘               └──────┬───────┘
┌─────────────────┐                      │ per-session
│  Claude Code B  │◄────────────────────►│ state via
│  (agent)        │                      │ Context
└─────────────────┘               ┌──────┴───────┐
                                  │  Textual TUI  │  ◄─── Frontend API :8445
                                  │  (tui.py)     │       (SSE + REST)
                                  └──────┬───────┘            │
                                         │              ┌─────┴──────┐
                              ┌──────────┼──────────┐   │ Android App│
                              │          │          │   │ (Compose)  │
                         scroll wheel  keyboard   TTS   └────────────┘
                         (smart ring)  (optional) audio
```

- **MCP Server** (`server.py`): FastMCP tools via streamable-http on port 8444. Decoupled from frontend via `Frontend` protocol
- **Frontend API** (`api.py`): REST + SSE on port 8445 for remote frontends (Android app)
- **TUI** (`tui.py`): Textual app — tabbed sessions, scroll/keyboard navigation, TTS, voice input
- **Android App** (`android/`): Jetpack Compose stateless frontend — displays choices, sends selections, mic button
- **Multi-session**: Each agent gets its own tab with independent state
- **TTS pipeline**: `tts` CLI → WAV → `paplay` via PulseAudio. Streaming mode for lower latency
- **Config system**: `~/.config/io-mcp/config.yml` merged with local `.io-mcp.yml`
- **Haptic feedback**: `termux-vibrate` on scroll (30ms) and selection (100ms)
- **Ambient mode**: Escalating status updates during agent silence — configurable timing and messages

## Source Layout

```
src/io_mcp/
├── __main__.py   # CLI entry, server startup, Frontend adapter, watchdog
├── api.py        # Frontend API: EventBus, SSE, REST endpoints, HTTP server
├── cli.py        # CLI tool: io-mcp-msg for sending messages to sessions
├── config.py     # IoMcpConfig: YAML loading, env expansion, validation, key bindings
├── server.py     # MCP tools, Frontend/TTSBackend protocols, create_mcp_server()
├── session.py    # Session/SpeechEntry/HistoryEntry dataclasses, SessionManager
├── settings.py   # Settings: wraps IoMcpConfig with property accessors
├── tui.py        # Textual app: choices, TTS, voice input, settings, extras
└── tts.py        # TTSEngine: caching, streaming, pregeneration, playback
android/
├── app/src/main/java/com/iomcp/app/MainActivity.kt  # Compose UI
├── flake.nix     # Nix dev shell for Android SDK
└── build files   # Gradle build configuration
```

## MCP Tools

| Tool | Description |
|------|-------------|
| `present_choices(preamble, choices)` | Show scroll-wheel choices, block until selection |
| `present_multi_select(preamble, choices)` | Checkable list — toggle items, submit with Done |
| `speak(text)` | Blocking TTS narration |
| `speak_async(text)` | Non-blocking TTS narration |
| `speak_urgent(text)` | High-priority TTS — interrupts current playback |
| `rename_session(name)` | Set descriptive tab name |
| `run_command(command)` | Run shell command with user approval |
| `set_speed(speed)` | Change TTS speed (0.5-2.5) |
| `set_voice(voice)` | Change TTS voice |
| `set_tts_model(model)` | Switch TTS model (resets voice) |
| `set_stt_model(model)` | Switch STT model |
| `set_emotion(emotion)` | Set emotion preset or custom instructions |
| `get_settings()` | Read current settings as JSON |
| `reload_config()` | Re-read config from disk, clear TTS cache |
| `pull_latest()` | Git pull --rebase + hot reload |

All tool responses include any queued user messages.

## Frontend API (port 8445)

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/events` | GET | SSE event stream |
| `/api/sessions` | GET | List active sessions |
| `/api/settings` | GET | Current settings |
| `/api/health` | GET | Health check |
| `/api/sessions/:id/select` | POST | Send a selection |
| `/api/sessions/:id/message` | POST | Queue a user message |
| `/api/sessions/:id/highlight` | POST | Set highlight index |
| `/api/sessions/:id/key` | POST | Send key event (j/k/enter/space) |
| `/api/message` | POST | Broadcast message to all/active/specific session |

SSE events: `choices_presented`, `speech_requested`, `selection_made`, `recording_state`, `session_created`, `session_removed`

## Configuration

```yaml
config:
  colorScheme: nord         # nord, tokyo-night, catppuccin, dracula
  tts:
    model: mai-voice-1
    voice: en-US-Noa:MAI-Voice-1
    speed: 1.3
    emotion: shy            # default: soft whisper style
    voiceRotation: []       # cycle voices across agent tabs
    emotionRotation: []
  stt:
    model: whisper
    realtime: false
  session:
    cleanupTimeoutSeconds: 300
  ambient:                    # periodic status updates during agent silence
    enabled: true
    initialDelaySecs: 30      # first update after 30s of silence
    repeatIntervalSecs: 45    # subsequent updates every 45s
  agents:                     # spawning new Claude Code agents
    defaultWorkdir: ~
    hosts:                    # remote hosts for agent spawning
      - name: Desktop
        host: desktop.local
        workdir: ~/projects/myapp
  keyBindings:              # all keys are configurable
    cursorDown: j
    cursorUp: k
    select: enter
    voiceInput: space
    freeformInput: i
    queueMessage: m
    settings: s
    nextTab: l
    prevTab: h
    hotReload: r

emotionPresets:
  shy: "Speak in a soft, quiet whisper. Hesitant and gentle."
  happy: "Speak in a warm, cheerful tone."
  calm: "Speak in a soothing, relaxed tone."
  # ... plus excited, serious, friendly, neutral, storyteller, gentle

extraOptions:               # project-local in .io-mcp.yml
  - title: Commit and push
    description: Stage, commit, and push changes
    silent: true

quickActions:               # macros accessible via 'x' key
  - key: "!"
    label: Commit and push
    action: message         # queue message to agent
    value: "commit all changes and push"
  - key: "@"
    label: Run tests
    action: command         # run shell command
    value: "pytest tests/ -q"
```

## Keyboard Shortcuts (all configurable)

| Key | Action |
|-----|--------|
| `j`/`k`/`↑`/`↓` | Navigate choices |
| `Enter` | Select / stop recording |
| `1`-`9` | Instant select by number |
| `h`/`l` | Previous/Next tab |
| `n` | Next tab with open choices |
| `u` | Undo last selection (re-present choices) |
| `/` | Filter choices by typing |
| `t` | Spawn new Claude Code agent (local or remote) |
| `x` | Quick actions (configurable macros) |
| `c` | Toggle conversation mode (continuous voice chat) |
| `d` | Dashboard (overview of all agent sessions) |
| `i` | Freeform text input |
| `m` | Queue message for agent |
| `space` | Voice input (toggle recording) |
| `s` | Settings menu |
| `p`/`P` | Replay prompt / all options |
| `r` | Hot reload |
| `q` | Quit |

## Android App

Native Jetpack Compose frontend that connects to the TUI via the Frontend API on port 8445.

**Features:**
- SSE event streaming for real-time choice/speech/session updates
- Touch selection with haptic feedback
- Scroll-to-highlight sync (triggers TUI TTS readout)
- Keyboard support: j/k/enter/space forwarded to TUI
- Volume buttons for scrolling
- Mic button for voice recording (triggers TUI STT)
- Message text input field
- Notification sound on new choices
- Recording state sync (mic button turns red)
- Session tab sync via SSE events
- Configurable server endpoint via SharedPreferences
- No TTS — all audio handled by TUI (avoids duplicates)

**Building:**
```bash
cd android && nix develop path:. --command gradle assembleDebug
adb install app/build/outputs/apk/debug/app-debug.apk
```

## Building

```bash
nix develop          # Dev shell
nix build            # Build package
uv run io-mcp        # Run directly
uv run pytest tests/ # Run tests (60 tests)
```

## CLI Tools

```bash
io-mcp-msg "check this"              # Broadcast to all agent sessions
io-mcp-msg --active "look at this"   # Send to focused session only
io-mcp-msg -s SESSION_ID "message"   # Send to specific session
io-mcp-msg --list                    # List active sessions
io-mcp-msg --health                  # Check io-mcp health
echo "msg" | io-mcp-msg              # Pipe from stdin
```

## Important Notes for Agents

### NEVER Stop Without Presenting Choices

**Every single response MUST end with a `present_choices()` call.** The user interacts entirely through the scroll wheel and choices — if you stop without presenting choices, the app becomes unresponsive and the user is stuck. There are NO exceptions to this rule.

- After completing a task → present choices for what to do next
- After an error → present choices: retry, skip, investigate, etc.
- After answering a question → present choices for follow-up actions
- When you have nothing to do → present choices: "Keep building", "Review changes", "Something else"
- **NEVER** end your turn with just `speak()` or text output — always follow with `present_choices()`

### Speech is Critical — Never Go Silent

**ALWAYS narrate what you're doing via `speak_async()`.** The user is listening through earphones and cannot see the screen. Long silences feel broken. Follow these rules:

- **Before every tool call**: Say what you're about to do. "Reading the config file." "Running tests." "Building the APK."
- **Before long operations**: Explain what will happen. "This build will take a minute. I'll update you when it's done."
- **After completing work**: Confirm the result. "Tests passed." "Build succeeded." "File updated."
- **When presenting choices**: The `present_choices` tool handles its own TTS, but prefix it with context via `speak_async()`.
- **Use `speak_async()` for narration** (non-blocking) and `speak()` only when you need to wait for the user to hear it before proceeding.
- **Use `speak_urgent()` for critical alerts** that must interrupt current audio.

### Other Important Notes

- Config is at `~/.config/io-mcp/config.yml` — use `set_*` tools or `reload_config` to change settings
- Local `.io-mcp.yml` in cwd is merged on top (for project-specific extra options)
- Per-session voice/emotion rotation: set `voiceRotation`/`emotionRotation` lists in config
- Key bindings are configurable in `config.keyBindings`
- Use `rename_session()` on connect to set a descriptive tab name
- Use `run_command()` to execute shell commands on the server device with user approval
- Use `present_multi_select()` for checkable batch selections
- User messages queued via `m` key appear in your next tool response — check for them
- MCP server auto-restarts up to 5 times on crash (watchdog with exponential backoff)
- All 15 MCP tools wrapped with error safety — single tool errors don't crash the server
- Config validated on load with specific warnings for invalid references
- Ambient mode: escalating TTS updates during silence (30s initial, then every 45s with context). Configurable in `config.ambient`
- Agent activity indicator shows last speech in TUI
- Streaming TTS used automatically for blocking speak calls — lower latency
- Haptic feedback auto-detected via `termux-vibrate`; no-op on non-Android
