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
- **Background thread**: MCP SSE server on port 8444 via FastMCP + uvicorn
- **Three MCP tools**: `speak(text)` (blocking), `speak_async(text)` (non-blocking), and `present_choices(preamble, choices)`
- **Multi-session**: Each SSE client gets its own session tab with independent choices, selection events, and speech inbox
- **TTS pipeline**: `tts` CLI (gpt-4o-mini-tts) or `espeak-ng` → WAV → `paplay` via PulseAudio TCP bridge

## Source Layout

```
src/io_mcp/
├── __main__.py   # CLI entry point, MCP server setup, arg parsing
├── session.py    # Session/SpeechEntry dataclasses, SessionManager
├── tui.py        # Textual app: tabbed sessions, choices, settings, voice, extras
└── tts.py        # TTSEngine: pregeneration, caching, blocking/async playback
```

### Key modules

- **`__main__.py`**: Parses CLI flags, creates TTSEngine instances (main + freeform), instantiates IoMcpApp, starts MCP server thread (or demo loop). Manages PID file at `/tmp/io-mcp.pid`. Uses `Context` injection from FastMCP to route tool calls to per-session state.
- **`session.py`**: Per-session state management:
  - `Session`: Dataclass holding per-session choices, preamble, selection_event, speech_log, unplayed_speech, scroll position, and input mode flags.
  - `SpeechEntry`: Timestamped speech text with played/unplayed tracking.
  - `SessionManager`: Thread-safe manager with `get_or_create()`, `remove()`, `next_tab()`, `prev_tab()`, `next_with_choices()`, `focused()`, and `tab_bar_text()`.
- **`tui.py`**: Textual `App` subclass with multiple modes:
  - **Multi-session tabs**: Tab bar shows all sessions, `h`/`l` to navigate, `n` to jump to next tab with open choices. State swapped on tab switch — one set of widgets, many session states.
  - **Speech priority**: Foreground tab speech plays immediately and interrupts background. Background speech queued in `session.unplayed_speech`, played when foreground is idle. Tab switch drains inbox then reads prompt+options.
  - **Speech log**: `speak()` calls display text in the UI as well as playing audio. Last 5 entries shown.
  - **Choice presentation**: `present_choices(session, preamble, choices)` blocking API. Reads preamble + titles, then all option descriptions sequentially. Scroll interrupts readout.
  - **Extras (negative indices)**: Hidden options at indices 0, -1, -2, -3 reached by scrolling up past option 1: record response, fast toggle, voice toggle, settings.
  - **Voice input** (`space`): Records via `stt` CLI, wraps transcription in `<transcription>` tags.
  - **Settings menu** (`s`): Speed, voice, provider settings. Global (not per-session).
  - **Prompt replay** (`p`/`P`): `p` replays preamble only, `P` replays preamble + all options.
  - **Freeform input** (`i`): Type text response, TTS reads back on delimiter.
  - Scroll debounce, dwell-to-select, 1-9 instant select, invert scroll.
- **`tts.py`**: `TTSEngine` with two backends (`--local` for espeak-ng, default gpt-4o-mini-tts). Pregenerates all choice audio in parallel via `ThreadPoolExecutor`. `speak()` blocks, `speak_async()` doesn't. Cache at `/tmp/io-mcp-tts-cache/`.

## Plugin / Agent Structure

```
.claude/agents/io-mcp.md          # Agent definition (YAML frontmatter + system prompt)
.claude/hooks/enforce-choices.sh   # Stop hook — blocks unless present_choices() was called
.claude/hooks/nudge-speak.sh       # PreToolUse hook — reminds to speak() before tools
.claude/skills/io-mcp/SKILL.md    # Skill definition loaded by /io-mcp command
.claude-plugin/plugin.json         # Plugin metadata for marketplace discovery
```

Installed via `cosmos-plugins` marketplace at `~/cosmos/.claude-plugin/marketplace.json`.
Invoked via `claude --agent io-mcp` or `/io-mcp` skill command.

## Runtime Dependencies

- **paplay** (pulseaudio) — audio playback
- **espeak-ng** — local/fast TTS fallback
- **tts** CLI (from `~/mono/tools/tts`) — gpt-4o-mini-tts API wrapper (optional, falls back to espeak-ng)
- **stt** CLI (from `~/mono/tools/stt`) — speech-to-text for voice input (optional)
- **PulseAudio TCP bridge** — `PULSE_SERVER=127.0.0.1` connecting proot to native Termux PulseAudio

## Building

```bash
# Dev shell (editable install + runtime deps)
nix develop

# Build package
nix build

# Run directly
nix run

# Or via uv (without Nix)
uv run io-mcp
```

## CLI Flags

| Flag | Default | Description |
|------|---------|-------------|
| `--local` | off | Use espeak-ng instead of gpt-4o-mini-tts |
| `--port` | 8444 | SSE server port |
| `--host` | 0.0.0.0 | SSE server bind address |
| `--dwell` | 0 (off) | Auto-select after N seconds |
| `--scroll-debounce` | 0.15 | Min seconds between scroll events |
| `--append-option` | "More options" | Always append this choice (repeatable). Format: `title` or `title::description` |
| `--demo` | off | Demo mode — test choices loop, no MCP server |
| `--freeform-tts` | local | TTS backend for freeform typing readback (api\|local) |
| `--freeform-tts-speed` | 1.6 | Speed multiplier for freeform TTS |
| `--freeform-tts-delimiters` | " .,;:!?" | Chars that trigger typing readback |
| `--invert` | off | Reverse scroll direction interpretation |

## Keyboard Shortcuts

| Key | Action |
|-----|--------|
| `j`/`k`/`↑`/`↓` | Navigate choices |
| `Enter` | Select highlighted choice |
| `1`-`9` | Instant select by number |
| `h` | Previous tab |
| `l` | Next tab |
| `n` | Next tab with open choices |
| `i` | Freeform text input mode |
| `space` | Voice input (toggle recording) |
| `s` | Open/close settings menu |
| `p` | Replay prompt |
| `P` | Replay prompt + all options |
| `Escape` | Cancel current mode |
| `q` | Quit |

## Environment Variables for TTS

| Variable | Default | Description |
|----------|---------|-------------|
| `TTS_SPEED` | 1.0 | TTS speed multiplier |
| `TTS_PROVIDER` | openai | TTS provider: openai or azure-speech |
| `OPENAI_TTS_VOICE` | sage | OpenAI TTS voice |
| `AZURE_SPEECH_VOICE` | en-US-Noa:MAI-Voice-1 | Azure Speech voice |
| `PULSE_SERVER` | 127.0.0.1 | PulseAudio server for audio playback |

## Important Notes for Agents

- The hardcoded `PORTAUDIO_LIB` path in `tts.py` is specific to the Nix-on-Droid environment
- The `TTS_TOOL_DIR` in `tts.py` points to `~/mono/tools/tts` — a private repo
- PID file at `/tmp/io-mcp.pid` is used by hooks to detect if io-mcp is running
- Each session has its own `threading.Event` for cross-thread sync between MCP server and Textual app
- Session identity is derived from `id(ctx.session)` where `ctx: Context` is injected by FastMCP
- Audio cache is shared at `/tmp/io-mcp-tts-cache/` — cleared on cleanup
- Settings changes (speed, voice, provider) clear the TTS cache to regenerate with new params
- Extra options (negative indices) are local-only and not sent back to the Claude instance
- Speech priority: foreground tab interrupts background; background speech queued and played opportunistically
