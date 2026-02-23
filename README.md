# io-mcp

**Hands-free Claude Code interaction via scroll wheel and earphones.**

An MCP server that lets you control Claude Code using only a smart ring (scroll wheel) and earphones (TTS). No keyboard needed. Designed for Nix-on-Droid on Galaxy S24 Ultra, with a native Android companion app.

```
                    ┌─────────────┐
                    │  Smart Ring  │ ──── scroll wheel input
                    └──────┬──────┘
                           │
┌──────────┐        ┌──────▼───────┐        ┌──────────────┐
│ Claude A │◄──────►│   io-mcp     │◄──────►│  Android App │
│ Claude B │◄──────►│   TUI + MCP  │  SSE   │  (Compose)   │
│ Claude C │◄──────►│              │        └──────────────┘
└──────────┘        └──────┬───────┘
                           │
                    ┌──────▼──────┐
                    │  Earphones  │ ──── TTS audio output
                    └─────────────┘
```

## Features

### Core
- **Scroll-wheel navigation** — browse choices by scrolling, select with a click
- **TTS narration** — every choice, status update, and agent response is spoken aloud
- **Multi-agent tabs** — multiple Claude Code instances, each with its own session tab
- **Voice input** — speak your replies via STT (press space to record)

### Interaction Modes
- **Choice mode** — scroll through options, hear each one, select with Enter
- **Conversation mode** (`c`) — continuous voice back-and-forth, no menus needed
- **Freeform input** (`i`) — type a reply with espeak readback as you type
- **Message queue** (`m`) — queue messages for the agent's next response

### Navigation & Control
- **Undo selection** (`u`) — go back and re-pick if you scrolled past the right option
- **Choice filter** (`/`) — type to narrow choices by label or summary
- **Dashboard** (`d`) — mission control overview of all agent sessions
- **Agent log** (`g`) — scrollable transcript of everything the focused agent has said
- **Agent spawner** (`t`) — launch new Claude Code instances (local or remote via SSH)
- **Quick actions** (`x`) — configurable macros from `.io-mcp.yml`
- **Tab navigation** (`h`/`l`) — switch between agent tabs

### Polish
- **4 color schemes** — Nord (default), Tokyo Night, Catppuccin, Dracula
- **Audio cues** — subtle chimes for choices arriving, selection, recording, agent connect
- **Ambient mode** — escalating TTS updates during long agent silence
- **Haptic feedback** — vibration on scroll and selection (Android/Termux)

### Infrastructure
- **CLI message tool** (`io-mcp-msg`) — send messages to agents from another terminal
- **Frontend API** — REST + SSE on port 8445 for remote clients
- **Android app** — Jetpack Compose companion app (touch, keyboard, mic, notifications)
- **Configurable** — YAML config with env var expansion, per-project overrides
- **Watchdog** — auto-restart on crash with exponential backoff

## Quick Start

```bash
# Install and run
nix develop
uv run io-mcp

# Or with options
uv run io-mcp --port 9000 --dwell 5

# Demo mode (no agent needed)
uv run io-mcp --demo
```

## Configuration

Config lives at `~/.config/io-mcp/config.yml` with optional per-project `.io-mcp.yml` overrides.

```yaml
config:
  colorScheme: nord         # nord, tokyo-night, catppuccin, dracula
  tts:
    model: gpt-4o-mini-tts
    speed: 1.3
    emotion: happy
  stt:
    model: whisper
  ambient:
    enabled: true
    initialDelaySecs: 30
    repeatIntervalSecs: 45
  agents:
    defaultWorkdir: ~
    hosts:
      - name: Desktop
        host: desktop.local
        workdir: ~/projects

# Project-local quick actions
quickActions:
  - key: "!"
    label: Commit and push
    action: message
    value: "commit all changes and push"
  - key: "@"
    label: Run tests
    action: command
    value: "pytest tests/ -q"
```

## Keyboard Shortcuts

All keys are configurable via `config.keyBindings`.

| Key | Action |
|-----|--------|
| `j`/`k` | Navigate choices |
| `Enter` | Select / stop recording |
| `1`-`9` | Instant select by number |
| `u` | Undo last selection |
| `/` | Filter choices |
| `t` | Spawn new agent |
| `x` | Quick actions |
| `c` | Conversation mode |
| `d` | Dashboard |
| `g` | Agent log |
| `h`/`l` | Switch tabs |
| `space` | Voice input |
| `i` | Freeform text input |
| `m` | Queue message |
| `s` | Settings |
| `r` | Hot reload |

## CLI Tools

```bash
io-mcp-msg "check this"              # Broadcast to all agents
io-mcp-msg --active "look at this"   # Send to focused agent
io-mcp-msg -s SESSION_ID "message"   # Send to specific agent
io-mcp-msg --list                    # List active sessions
io-mcp-msg --health                  # Health check
echo "msg" | io-mcp-msg              # Pipe from stdin
```

## Architecture

```
src/io_mcp/
├── __main__.py   # CLI entry, server startup, watchdog
├── api.py        # Frontend API: SSE events, REST endpoints
├── cli.py        # io-mcp-msg CLI tool
├── config.py     # YAML config with env expansion
├── server.py     # MCP tools, Frontend protocol
├── session.py    # Per-agent session state
├── settings.py   # Runtime settings wrapper
├── tui/          # Textual TUI package
│   ├── app.py    # Main app: choices, TTS, voice input, actions
│   ├── themes.py # Color schemes and CSS generation
│   └── widgets.py # ChoiceItem, DwellBar, extras
└── tts.py        # TTS engine: caching, streaming, tones
android/          # Jetpack Compose companion app
```

### MCP Tools (15 total)

| Tool | Description |
|------|-------------|
| `present_choices` | Show scroll-wheel choices, block until selection |
| `present_multi_select` | Checkable multi-select list |
| `speak` / `speak_async` | TTS narration (blocking / non-blocking) |
| `speak_urgent` | High-priority TTS, interrupts current audio |
| `set_speed` / `set_voice` / `set_emotion` | Runtime TTS config |
| `set_tts_model` / `set_stt_model` | Switch models |
| `rename_session` | Set descriptive tab name |
| `run_command` | Execute shell command with user approval |
| `get_settings` / `reload_config` | Read/refresh settings |
| `pull_latest` | Git pull + hot reload |

### Frontend API (port 8445)

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/events` | GET | SSE event stream |
| `/api/sessions` | GET | List sessions |
| `/api/health` | GET | Health check |
| `/api/message` | POST | Broadcast message |
| `/api/sessions/:id/select` | POST | Send selection |
| `/api/sessions/:id/message` | POST | Queue message |
| `/api/sessions/:id/key` | POST | Forward key event |

## Android App

Native Jetpack Compose frontend connecting via the Frontend API.

- SSE event streaming for real-time updates
- Touch selection with haptic feedback
- Keyboard support (j/k/enter/space forwarded to TUI)
- Volume buttons for scrolling
- Mic button for voice recording
- No TTS — all audio handled by TUI

```bash
cd android && nix develop path:. --command gradle assembleDebug
adb install app/build/outputs/apk/debug/app-debug.apk
```

## Development

```bash
nix develop          # Dev shell
nix build            # Build package
uv run io-mcp        # Run directly
uv run pytest tests/ # Run tests (106 tests)
```

## License

MIT
