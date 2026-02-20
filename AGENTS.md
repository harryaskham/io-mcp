# io-mcp — Agent Guide

MCP server providing hands-free Claude Code interaction via scroll wheel (smart ring) and earphones (TTS). Runs on Nix-on-Droid (Galaxy S24 Ultra, aarch64, Android 14).

## Architecture

```
┌─────────────────┐   SSE :8444   ┌──────────────┐
│  Claude Code    │◄────────────►│  MCP Server   │
│  (agent)        │               │  (FastMCP)    │
└─────────────────┘               └──────┬───────┘
                                         │ thread
                                  ┌──────┴───────┐
                                  │  Textual TUI  │
                                  │  (main thread) │
                                  └──────┬───────┘
                                         │
                              ┌──────────┼──────────┐
                              │          │          │
                         scroll wheel  keyboard   TTS audio
                         (smart ring)  (optional) (earphones)
```

- **Main thread**: Textual TUI — scroll/keyboard navigation, choice display
- **Background thread**: MCP SSE server on port 8444 via FastMCP + uvicorn
- **Two MCP tools**: `speak(text)` and `present_choices(preamble, choices)`
- **TTS pipeline**: `tts` CLI (gpt-4o-mini-tts) or `espeak-ng` → WAV → `paplay` via PulseAudio TCP bridge

## Source Layout

```
src/io_mcp/
├── __main__.py   # CLI entry point, MCP server setup, arg parsing
├── tui.py        # Textual app: ChoiceItem, IoMcpApp, input mode, scroll handling
└── tts.py        # TTSEngine: pregeneration, caching, blocking/async playback
```

### Key modules

- **`__main__.py`**: Parses CLI flags, creates TTSEngine instances (main + freeform), instantiates IoMcpApp, starts MCP server thread (or demo loop). Manages PID file at `/tmp/io-mcp.pid`.
- **`tui.py`**: Textual `App` subclass. `present_choices()` is the blocking API called from MCP — sets choices, pregenerates TTS, waits on `threading.Event` for user selection. Handles scroll debounce, dwell-to-select, `i` for freeform input, 1-9 for instant select.
- **`tts.py`**: `TTSEngine` with two backends (`--local` for espeak-ng, default gpt-4o-mini-tts). Pregenerates all choice audio in parallel via `ThreadPoolExecutor`. `speak()` blocks, `speak_async()` doesn't. Cache at `/tmp/io-mcp-tts-cache/`.

## Plugin / Agent Structure

```
agents/io-mcp.md          # Agent definition (YAML frontmatter + system prompt)
hooks/enforce-choices.sh   # Stop hook — blocks unless present_choices() was called
hooks/nudge-speak.sh       # PreToolUse hook — reminds to speak() before tools
.claude-plugin/plugin.json # Plugin metadata for marketplace discovery
.claude/skills/io-mcp/SKILL.md  # Skill definition loaded by /io-mcp command
```

Installed via `cosmos-plugins` marketplace at `~/cosmos/.claude-plugin/marketplace.json`.
Invoked via `claude --agent io-mcp` or `/io-mcp` skill command.

## Runtime Dependencies

- **paplay** (pulseaudio) — audio playback
- **espeak-ng** — local/fast TTS fallback
- **tts** CLI (from `~/mono/tools/tts`) — gpt-4o-mini-tts API wrapper (optional, falls back to espeak-ng)
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
| `--append-option` | "More options" | Always append this choice (repeatable) |
| `--demo` | off | Demo mode — test choices loop, no MCP server |
| `--freeform-tts` | local | TTS backend for freeform typing readback (api\|local) |
| `--freeform-tts-speed` | 1.6 | Speed multiplier for freeform TTS |
| `--freeform-tts-delimiters` | " .,;:!?" | Chars that trigger typing readback |
| `--speed` | 1.2 | Speed multiplier for OpenAI TTS |
| `--voice` | sage | OpenAI TTS voice name |
| `--invert` | off | Reverse scroll direction interpretation |

## Important Notes for Agents

- The hardcoded `PORTAUDIO_LIB` path in `tts.py` is specific to the Nix-on-Droid environment
- The `TTS_TOOL_DIR` in `tts.py` points to `~/mono/tools/tts` — a private repo
- PID file at `/tmp/io-mcp.pid` is used by hooks to detect if io-mcp is running
- The `tui.py` uses `threading.Event` for cross-thread sync between MCP server and Textual app
- Audio cache is per-session at `/tmp/io-mcp-tts-cache/` — cleared on cleanup
