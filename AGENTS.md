# io-mcp — Agent Guide

MCP server providing hands-free Claude Code interaction via scroll wheel (smart ring) and earphones (TTS). Runs on Nix-on-Droid (Galaxy S24 Ultra, aarch64, Android 14).

## Architecture

```
┌─────────────────┐   SSE :8444   ┌──────────────┐
│  Claude Code A  │◄────────────►│  MCP Server   │
│  (agent)        │               │  (FastMCP)    │
└─────────────────┘               └──────┬───────┘
┌─────────────────┐                      │ per-session
│  Claude Code B  │◄────────────────────►│ state via
│  (agent)        │                      │ Context
└─────────────────┘               ┌──────┴───────┐
                                  │  Textual TUI  │
                                  │  (main thread) │
                                  │  [tabbed UI]   │
                                  └──────┬───────┘
                                         │
                              ┌──────────┼──────────┐
                              │          │          │
                         scroll wheel  keyboard   TTS audio
                         (smart ring)  (optional) (earphones)
```

- **Main thread**: Textual TUI — tabbed sessions, scroll/keyboard navigation, choice display
- **Background thread**: MCP streamable-http server on port 8444 via FastMCP + uvicorn
- **MCP tools**: `speak`, `speak_async`, `speak_urgent`, `present_choices`, plus settings/session management tools
- **Multi-session**: Each client gets its own session tab with independent choices, selection events, speech inbox, message queue, and optional voice/emotion overrides
- **TTS pipeline**: `tts` CLI → WAV → `paplay` via PulseAudio TCP bridge. Supports streaming (pipe) and cached playback
- **Config system**: `~/.config/io-mcp/config.yml` merged with local `.io-mcp.yml`. Defines providers, models, voices, emotions, session settings, and extra options
- **Haptic feedback**: `termux-vibrate` via `termux-exec` for tactile scroll/selection feedback
- **User message inbox**: Users can queue messages (key `m`) that get attached to the next MCP response

## Source Layout

```
src/io_mcp/
├── __main__.py   # CLI entry point, MCP server + tools, arg parsing
├── config.py     # IoMcpConfig: YAML config loading, env expansion, CLI arg generation
├── session.py    # Session/SpeechEntry/HistoryEntry dataclasses, SessionManager
├── settings.py   # Settings: runtime settings backed by IoMcpConfig
├── tui.py        # Textual app: tabbed sessions, choices, settings, voice, extras
└── tts.py        # TTSEngine: pregeneration, caching, streaming, blocking/async playback
```

### Key modules

- **`config.py`**: Configuration system backed by YAML files:
  - Loads `~/.config/io-mcp/config.yml` (global) merged with `.io-mcp.yml` (local/project)
  - Shell variable expansion: `${VAR}` and `${VAR:-default}` in config strings
  - Defines providers (baseUrl, apiKey), models (TTS, STT, realtime), emotion presets
  - Generates CLI args for `tts` and `stt` tools with explicit flags
  - Supports per-session voice/emotion rotation for multi-agent setups
  - Session settings: configurable cleanup timeout (`cleanupTimeoutSeconds`)
  - Config mutations auto-save to disk
- **`__main__.py`**: Parses CLI flags, loads `IoMcpConfig`, creates TTSEngine instances, instantiates IoMcpApp, starts MCP server thread. Manages PID file and wake lock on Android.
  - MCP tools: `present_choices`, `speak`, `speak_async`, `speak_urgent`, `set_speed`, `set_voice`, `set_tts_model`, `set_stt_model`, `set_emotion`, `get_settings`, `rename_session`, `reload_config`, `pull_latest`
  - User message inbox: drains pending messages and attaches to MCP responses
- **`session.py`**: Per-session state management:
  - `Session`: Dataclass with choices, preamble, selection_event, speech_log, unplayed_speech, scroll position, input mode flags, voice/emotion overrides, selection history, pending messages, and activity tracking
  - `HistoryEntry`: Records past selections with timestamps
  - `SpeechEntry`: Speech events with priority levels (0=normal, 1=urgent)
  - `SessionManager`: Thread-safe manager with tab navigation, session lifecycle, and stale session cleanup
- **`settings.py`**: `Settings` class wrapping `IoMcpConfig` with property accessors for speed, voice, model, emotion. Supports toggle operations
- **`tui.py`**: Textual `App` subclass with multiple modes:
  - **Multi-session tabs**: Tab bar shows all sessions, `h`/`l` to navigate, `n` to jump to next tab with open choices
  - **Speech priority**: Foreground tab plays immediately; background speech queued. Urgent (priority 1) interrupts current playback
  - **Streaming TTS**: Blocking speak calls pipe `tts` stdout directly to `paplay` for lower latency
  - **Choice presentation**: Blocking API. Reads preamble + titles, then descriptions. Scroll interrupts readout. Silent options skipped in readout
  - **Extras (negative indices)**: Hidden options above real choices: queue message, history, notifications, record response, fast toggle, voice toggle, settings, next/prev tab
  - **Voice input** (`space`/`Enter` to stop): Records via `termux-microphone-record`, transcribes via direct API or `stt` CLI
  - **Settings menu** (`s`): Speed, voice, emotion, TTS model, STT model. Works with or without agent connected
  - **Hot reload** (`r`): Reimports modules, monkey-patches methods, reloads config from disk
  - **Freeform input** (`i`): Type text response, TTS reads back on delimiter
  - **Message queue** (`m`): Type a message to queue for the agent's next MCP response
  - **Selection history**: Past selections tracked per session, reviewable via History extra option
  - **Haptic feedback**: `termux-vibrate` on scroll (30ms) and selection (100ms)
  - **Session auto-cleanup**: Periodic timer removes sessions inactive for configurable timeout (default 5 min)
- **`tts.py`**: `TTSEngine` with two backends (`--local` for espeak-ng, default uses `tts` CLI configured via config). Features:
  - Per-session voice/emotion overrides
  - Pregenerates choice audio in parallel
  - Cache includes model/voice/speed/emotion in key
  - Streaming playback: pipes `tts` stdout → `paplay` for lower time-to-first-audio
  - Priority support: urgent messages interrupt current playback

## Configuration

### Config files

- **Global**: `~/.config/io-mcp/config.yml` (created with defaults on first run)
- **Local**: `.io-mcp.yml` in the current working directory (merged on top, local wins)
- **CLI override**: `--config-file PATH`

### Config structure

```yaml
providers:
  openai:
    baseUrl: ${OPENAI_BASE_URL:-https://api.openai.com}
    apiKey: ${OPENAI_API_KEY}
  azure-foundry:
    baseUrl: ${AZURE_WCUS_ENDPOINT:-https://harryaskham-sandbox-ais-wcus.services.ai.azure.com}
    apiKey: ${AZURE_WCUS_API_KEY}
  azure-speech:
    baseUrl: ${AZURE_SPEECH_ENDPOINT:-https://eastus.tts.speech.microsoft.com}
    apiKey: ${AZURE_SPEECH_API_KEY}

models:
  stt:
    whisper: { provider: openai, supportsRealtime: true }
    gpt-4o-mini-transcribe: { provider: openai, supportsRealtime: true }
    mai-ears-1: { provider: azure-foundry, supportsRealtime: false }
  tts:
    gpt-4o-mini-tts:
      provider: openai
      voice: { default: sage, options: [alloy, ash, ballad, coral, echo, fable, onyx, nova, sage, shimmer, verse] }
    mai-voice-1:
      provider: azure-speech
      voice: { default: en-US-Noa:MAI-Voice-1, options: [en-US-Noa:MAI-Voice-1, en-US-Teo:MAI-Voice-1] }

config:
  tts:
    model: mai-voice-1
    voice: en-US-Noa:MAI-Voice-1
    speed: 1.3
    emotion: happy
    voiceRotation: []       # cycle voices across agent tabs
    emotionRotation: []     # cycle emotions across agent tabs
  stt:
    model: whisper
    realtime: false
  session:
    cleanupTimeoutSeconds: 300  # auto-remove inactive sessions after 5 min

emotionPresets:
  happy: "Speak in a warm, cheerful, and upbeat tone."
  calm: "Speak in a soothing, relaxed, and measured tone."
  excited: "Speak with high energy and enthusiasm."
  serious: "Speak in a focused, professional tone."
  friendly: "Speak in a warm, conversational tone."
  neutral: "Speak in a natural, even tone."
  storyteller: "Speak like a captivating narrator."
  gentle: "Speak softly and kindly."

# Project-local options (typically in .io-mcp.yml)
extraOptions:
  - title: Commit and push
    description: Stage, commit, and push changes
    silent: true    # not read aloud in intro, only when scrolled to
```

## MCP Tools

| Tool | Description |
|------|-------------|
| `present_choices(preamble, choices)` | Show scroll-wheel choices, block until selection |
| `speak(text)` | Blocking TTS narration |
| `speak_async(text)` | Non-blocking TTS narration |
| `speak_urgent(text)` | High-priority TTS — interrupts current playback |
| `set_speed(speed)` | Change TTS speed (0.5-2.5) |
| `set_voice(voice)` | Change TTS voice |
| `set_tts_model(model)` | Switch TTS model (resets voice) |
| `set_stt_model(model)` | Switch STT model |
| `set_emotion(emotion)` | Set emotion preset or custom instructions |
| `get_settings()` | Read current settings as JSON |
| `rename_session(name)` | Set a descriptive tab name (e.g., "Code Review") |
| `reload_config()` | Re-read config from disk, clear TTS cache |
| `pull_latest()` | Git pull --rebase + hot reload |

### User Message Inbox

All MCP tool responses may include a `user_messages` field containing messages the user queued while the agent was working:

```json
{
  "selected": "Build feature",
  "summary": "...",
  "user_messages": ["Remember to add tests", "Also update the docs"]
}
```

Agents should check for and acknowledge these messages.

## CLI Flags

| Flag | Default | Description |
|------|---------|-------------|
| `--local` | off | Use espeak-ng instead of API TTS |
| `--port` | 8444 | Server port |
| `--host` | 0.0.0.0 | Server bind address |
| `--dwell` | 0 (off) | Auto-select after N seconds |
| `--scroll-debounce` | 0.15 | Min seconds between scroll events |
| `--append-option` | "More options" | Always append this choice (repeatable) |
| `--append-silent-option` | (none) | Append option not read aloud (repeatable) |
| `--config-file` | ~/.config/io-mcp/config.yml | Config file path |
| `--demo` | off | Demo mode — test choices loop |
| `--freeform-tts` | local | TTS for typing readback (api\|local) |
| `--freeform-tts-speed` | 1.6 | Speed for freeform readback |
| `--freeform-tts-delimiters` | " .,;:!?" | Chars that trigger readback |
| `--invert` | off | Reverse scroll direction |

## Keyboard Shortcuts

| Key | Action |
|-----|--------|
| `j`/`k`/`↑`/`↓` | Navigate choices |
| `Enter` | Select / stop recording |
| `1`-`9` | Instant select by number |
| `h` | Previous tab |
| `l` | Next tab |
| `n` | Next tab with open choices |
| `i` | Freeform text input mode |
| `m` | Queue message for agent |
| `space` | Voice input (toggle recording) |
| `s` | Open/close settings menu |
| `p` | Replay prompt |
| `P` | Replay prompt + all options |
| `r` | Hot reload (reimport modules + reload config) |
| `Escape` | Cancel current mode |
| `q` | Quit |

## Runtime Dependencies

- **paplay** (pulseaudio) — audio playback
- **espeak-ng** — local/fast TTS fallback
- **tts** CLI (from `~/mono/tools/tts`) — API TTS wrapper (optional, falls back to espeak-ng)
- **stt** CLI (from `~/mono/tools/stt`) — speech-to-text (optional, also has direct API path)
- **ffmpeg** — audio format conversion for voice recording
- **termux-exec** — runs commands in native Termux from proot (for mic access)
- **termux-vibrate** — haptic feedback (optional, via termux-exec)
- **PulseAudio TCP bridge** — `PULSE_SERVER=127.0.0.1` connecting proot to native Termux

## Building

```bash
nix develop          # Dev shell
nix build            # Build package
nix run              # Run directly
uv run io-mcp        # Via uv (without Nix)
uv run pytest tests/ # Run tests (56 tests)
```

## Important Notes for Agents

- Config is at `~/.config/io-mcp/config.yml` — use `set_*` tools or `reload_config` to change settings
- Local `.io-mcp.yml` in cwd is merged on top (for project-specific extra options)
- TTS/STT tools are invoked with explicit CLI flags from config (not env vars)
- Per-session voice/emotion rotation: set `voiceRotation`/`emotionRotation` lists in config
- Silent extra options (`silent: true`) appear in the list but aren't read during intro
- Wake lock is acquired on Android startup to prevent device sleep
- Hot reload (`r`) reimports code modules AND reloads config from disk
- Audio cache key includes model + voice + speed + emotion — changes take effect immediately
- Session identity uses `mcp_session_id` from streamable-http transport
- Settings menu works independently of agent connection state
- Use `rename_session` to set a descriptive tab name for your agent
- Use `speak_urgent` for critical messages that must interrupt current audio
- Check `user_messages` in tool responses — the user may have queued notes while you were working
- Stale sessions are auto-cleaned after configurable timeout (default 5 min)
- Selection history is tracked per session and reviewable via the History extra option
