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
- **MCP tools**: `speak`, `speak_async`, `speak_urgent`, `present_choices`, `rename_session`, plus settings tools (`set_speed`, `set_voice`, `set_tts_model`, `set_stt_model`, `set_emotion`, `get_settings`, `reload_config`, `pull_latest`)
- **Multi-session**: Each client gets its own session tab with independent choices, selection events, speech inbox, message queue, and optional voice/emotion overrides
- **TTS pipeline**: `tts` CLI → WAV → `paplay` via PulseAudio TCP bridge. Streaming mode pipes tts stdout directly to paplay for lower latency
- **Config system**: `~/.config/io-mcp/config.yml` merged with local `.io-mcp.yml`. Defines providers, models, voices, emotions, session settings, and extra options
- **Haptic feedback**: `termux-vibrate` gives tactile feedback on scroll (30ms) and selection (100ms)

## Source Layout

```
src/io_mcp/
├── __main__.py   # CLI entry point, MCP server + tools, arg parsing
├── config.py     # IoMcpConfig: YAML config loading, env expansion, CLI arg generation
├── session.py    # Session/SpeechEntry/HistoryEntry dataclasses, SessionManager
├── settings.py   # Settings class: wraps IoMcpConfig with property accessors
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
  - Session settings: configurable cleanup timeout (`config.session.cleanupTimeoutSeconds`)
  - Config mutations auto-save to disk
- **`__main__.py`**: Parses CLI flags, loads `IoMcpConfig`, creates TTSEngine instances, instantiates IoMcpApp, starts MCP server thread. Manages PID file and wake lock on Android.
  - MCP tools: `present_choices`, `speak`, `speak_async`, `speak_urgent`, `rename_session`, `set_speed`, `set_voice`, `set_tts_model`, `set_stt_model`, `set_emotion`, `get_settings`, `reload_config`, `pull_latest`
  - User message inbox: queued messages are drained and attached to every MCP tool response
- **`session.py`**: Per-session state management:
  - `Session`: Dataclass with choices, preamble, selection_event, speech_log, unplayed_speech, scroll position, input mode flags, voice/emotion overrides, last_activity, history, and pending_messages
  - `HistoryEntry`: Records each selection with label, summary, preamble, and timestamp
  - `SpeechEntry`: Speech event with text, timestamp, played flag, and priority level
  - `SessionManager`: Thread-safe manager with tab navigation, session lifecycle, and stale session cleanup
- **`settings.py`**: `Settings` class wrapping `IoMcpConfig` with property accessors for speed, voice, emotion, models. Provides `toggle_fast()` and `toggle_voice()` shortcuts
- **`tui.py`**: Textual `App` subclass with multiple modes:
  - **Multi-session tabs**: Tab bar shows all sessions, `h`/`l` to navigate, `n` to jump to next tab with open choices
  - **Speech priority**: Foreground tab plays immediately; background speech queued and played when idle. Priority 1 (urgent) interrupts current playback
  - **Choice presentation**: Blocking API. Reads preamble + titles, then descriptions. Scroll interrupts readout. Silent options (from config) are skipped in readout but shown in UI
  - **Extras (negative indices)**: Hidden options above real choices: queue message, history, notifications, record response, fast toggle, voice toggle, settings, next/prev tab
  - **Voice input** (`space`/`Enter` to stop): Records via `termux-microphone-record`, transcribes via direct API or `stt` CLI
  - **Settings menu** (`s`): Speed, voice, emotion, TTS model, STT model. Works with or without agent connected
  - **Hot reload** (`r`): Reimports modules, monkey-patches methods, reloads config from disk
  - **Freeform input** (`i`): Type text response, TTS reads back on delimiter
  - **Message queue** (`m`): Type a message queued for the agent's next MCP response
  - **Choice history**: Tracks all selections per session, reviewable via History extra option
  - **Session auto-cleanup**: Removes inactive sessions after configurable timeout (default 5 min)
  - **Haptic feedback**: `termux-vibrate` on scroll (30ms) and selection (100ms) via `termux-exec`
- **`tts.py`**: `TTSEngine` with two backends (`--local` for espeak-ng, default uses `tts` CLI configured via config). Supports per-session voice/emotion overrides. Pregenerates choice audio in parallel. Cache includes model/voice/speed/emotion in key.
  - **Streaming TTS**: `speak_streaming()` pipes tts stdout directly to paplay, reducing time-to-first-audio for long narrations
  - **Stop** kills both playback and any streaming TTS process

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
| `speak_urgent(text)` | Blocking TTS that interrupts current playback |
| `rename_session(name)` | Set descriptive tab name (e.g., "Code Review") |
| `set_speed(speed)` | Change TTS speed (0.5-2.5) |
| `set_voice(voice)` | Change TTS voice |
| `set_tts_model(model)` | Switch TTS model (resets voice) |
| `set_stt_model(model)` | Switch STT model |
| `set_emotion(emotion)` | Set emotion preset or custom instructions |
| `get_settings()` | Read current settings as JSON |
| `reload_config()` | Re-read config from disk, clear TTS cache |
| `pull_latest()` | Git pull --rebase + hot reload |

All tool responses include any queued user messages (see Message Queue below).

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

## Extra Options (scroll up past first choice)

| Option | Description |
|--------|-------------|
| Queue message | Type a message queued for the agent's next response |
| History | Review the last 5 selections for this session |
| Notifications | Check Android notifications via termux-notification-list |
| Previous/Next tab | Switch between session tabs |
| Fast toggle | Toggle speed between current and 1.8x |
| Voice toggle | Cycle through available voices |
| Settings | Open settings menu |
| Record response | Voice input via microphone |

## User Message Queue

Users can queue messages while the agent is working (via `m` key or "Queue message" extra option). Messages are attached to the next MCP tool response the agent receives:

```json
{
  "selected": "Build feature",
  "summary": "...",
  "user_messages": ["Please compact when you get a chance", "Also check the tests"]
}
```

For non-JSON responses (speak/speak_async), messages are appended as text:
```
Spoke: hello world

[User messages queued while you were working:
- Please compact when you get a chance
- Also check the tests
]
```

## Runtime Dependencies

- **paplay** (pulseaudio) — audio playback
- **espeak-ng** — local/fast TTS fallback
- **tts** CLI (from `~/mono/tools/tts`) — API TTS wrapper (optional, falls back to espeak-ng)
- **stt** CLI (from `~/mono/tools/stt`) — speech-to-text (optional, also has direct API path)
- **ffmpeg** — audio format conversion for voice recording
- **termux-exec** — runs commands in native Termux from proot (for mic access, vibration)
- **termux-vibrate** — haptic feedback (auto-detected, graceful degradation if absent)
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
- TTS/STT tools are invoked with explicit CLI flags from config (not env vars)
- Per-session voice/emotion rotation: set `voiceRotation`/`emotionRotation` lists in config
- Silent extra options (`silent: true`) appear in the list but aren't read during intro
- Wake lock is acquired on Android startup to prevent device sleep
- Hot reload (`r`) reimports code modules AND reloads config from disk
- Audio cache key includes model + voice + speed + emotion — changes take effect immediately
- Session identity uses `mcp_session_id` from streamable-http transport
- Settings menu works independently of agent connection state
- Stale sessions (inactive 5+ min) are auto-removed; focused and active sessions are preserved
- Use `rename_session()` on connect to set a descriptive tab name
- User messages queued via `m` key appear in your next tool response — check for them
- Haptic feedback auto-detected via `termux-vibrate`; no-op on non-Android
- Streaming TTS used automatically for blocking speak calls — lower latency
