# io-mcp — Agent Guide

> **Note:** `CLAUDE.md` is a symlink to this file (`AGENTS.md`). Edit this file directly.

MCP server providing hands-free Claude Code interaction via scroll wheel (smart ring) and earphones (TTS). Runs on Nix-on-Droid (Galaxy S24 Ultra, aarch64, Android 14). Native Android companion app available.

## Architecture

```
┌─────────────────┐  streamable-http  ┌──────────────┐
│  Claude Code A  │◄────────────────►│  MCP Proxy    │ :8444
│  (agent)        │                   │  (proxy.py)   │
└─────────────────┘                   └──────┬───────┘
┌─────────────────┐                          │ HTTP POST
│  Claude Code B  │◄────────────────────────►│ /handle-mcp
│  (agent)        │                          │
└─────────────────┘                   ┌──────┴───────┐
                                      │  Backend      │ :8446
                                      │  (__main__.py) │
                                      └──────┬───────┘
                                             │
                                      ┌──────┴───────┐
                                      │  Textual TUI  │  ◄─── Frontend API :8445
                                      │  (tui/app.py) │       (SSE + REST)
                                      └──────┬───────┘            │
                                             │              ┌─────┴──────┐
                                  ┌──────────┼──────────┐   │ Android App│
                                  │          │          │   │ (Compose)  │
                             scroll wheel  keyboard   TTS   └────────────┘
                             (smart ring)  (optional) audio
```

- **MCP Proxy** (`proxy.py`): Thin FastMCP proxy on :8444. Agents connect here. Survives backend restarts.
- **Backend** (`__main__.py`): Main process with TUI, TTS, session logic. Exposes /handle-mcp on :8446 for the proxy.
- **Frontend API** (`api.py`): REST + SSE on :8445 for remote frontends (Android app)
- **TUI** (`tui/app.py`): Textual app — tabbed sessions, scroll/keyboard navigation, TTS, voice input
- **Android App** (`android/`): Jetpack Compose stateless frontend — displays choices, sends selections, mic button
- **Multi-session**: Each agent gets its own tab with independent state
- **TTS pipeline**: `tts` CLI → WAV → `paplay` via PulseAudio. Streaming mode for lower latency
- **Config system**: `~/.config/io-mcp/config.yml` merged with local `.io-mcp.yml`
- **Haptic feedback**: `termux-vibrate` on scroll (30ms) and selection (100ms). Vibration patterns for semantic events. **Disabled by default** — enable via `config.haptic.enabled: true`
- **Ambient mode**: Escalating status updates during agent silence — exponential backoff after 4th update

## Source Layout

```
src/io_mcp/
├── __main__.py       # CLI entry, two-process startup, tool dispatcher, backend HTTP
├── api.py            # Frontend API: EventBus, SSE, REST endpoints, HTTP server
├── backend.py        # Backend HTTP server: /handle-mcp, /health endpoints
├── cli.py            # CLI tool: io-mcp-msg for sending messages to sessions
├── config.py         # IoMcpConfig: YAML loading, env expansion, validation, key bindings
├── notifications.py  # Webhook notifications: ntfy, Slack, Discord, generic webhooks
├── proxy.py          # Thin MCP proxy: forwards tool calls to backend, survives restarts
├── server.py         # MCP tool definitions (used by tests; proxy.py is production path)
├── session.py        # Session/SpeechEntry/HistoryEntry dataclasses, SessionManager
├── settings.py       # Settings: wraps IoMcpConfig with property accessors
├── state.py          # Persistent UI state: toggle states, preferences (state.json)
├── tui/
│   ├── __init__.py
│   ├── app.py        # Textual app: choices, TTS, voice input, settings, extras
│   ├── settings_menu.py  # Settings menu mixin: speed, voice, emotion, theme
│   ├── views.py      # Dashboard, timeline, pane view, help screen, session actions
│   ├── voice.py      # Voice recording and transcription mixin
│   ├── themes.py     # Color schemes (nord, tokyo-night, catppuccin, dracula) + CSS
│   └── widgets.py    # ChoiceItem, DwellBar, EXTRA_OPTIONS
└── tts.py            # TTSEngine: caching, streaming, pregeneration, chimes, playback
android/
├── app/src/main/java/com/iomcp/app/MainActivity.kt  # Compose UI
├── flake.nix         # Nix dev shell for Android SDK
└── build files       # Gradle build configuration
```

## MCP Tools

| Tool | Description |
|------|-------------|
| `register_session(cwd, hostname, ...)` | Register agent with environment metadata (call first!) |
| `present_choices(preamble, choices)` | Show scroll-wheel choices, block until selection |
| `present_multi_select(preamble, choices)` | Checkable list — toggle items, submit with Done |
| `speak(text)` | Blocking TTS narration |
| `speak_async(text)` | Non-blocking TTS narration |
| `speak_urgent(text)` | High-priority TTS — interrupts current playback |
| `rename_session(name)` | Set descriptive tab name |
| `run_command(command)` | Run shell command with user approval |
| `request_close(reason)` | Request closing session with user confirmation |
| `set_speed(speed)` | Change TTS speed (0.5-2.5) |
| `set_voice(voice)` | Change TTS voice |
| `set_tts_model(model)` | Switch TTS model (resets voice) |
| `set_stt_model(model)` | Switch STT model |
| `set_emotion(emotion)` | Set emotion preset or custom instructions |
| `get_settings()` | Read current settings as JSON |
| `get_logs(lines)` | Get recent TUI error, proxy, and speech logs for debugging |
| `get_sessions()` | List all active agent sessions with status, health, metadata |
| `get_speech_history(lines, session)` | Get TTS speech log and selection history |
| `get_current_choices(session)` | Get the choices currently displayed to the user |
| `get_tui_state()` | Capture full TUI screen content and UI mode |
| `reload_config()` | Re-read config from disk, clear TTS cache |
| `pull_latest()` | Git pull --rebase + config refresh (restart TUI for code changes) |
| `request_restart()` | Restart backend (TUI reloads, proxy stays) |
| `request_proxy_restart()` | Restart proxy (breaks MCP connections) |
| `check_inbox()` | Poll for queued user messages without waiting |

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

Config merge order (later takes precedence):
1. Built-in defaults
2. `~/.config/io-mcp/config.yml` (user config)
3. `.io-mcp.yml` in cwd (project-local, checked into repo)
4. `.io-mcp.local.yml` in cwd (personal overrides, gitignored)
5. CLI flags (`--speed`, `--voice`, etc.)

```yaml
# Named voice presets — use these names in config.tts.voice, uiVoice, voiceRotation
voices:
  sage:
    provider: openai
    model: gpt-4o-mini-tts
    voice: sage
  noa:
    provider: openai
    model: azure/speech/azure-tts
    voice: en-US-Noa:MAI-Voice-1
  teo:
    provider: openai
    model: azure/speech/azure-tts
    voice: en-US-Teo:MAI-Voice-1
  # ... alloy, ash, ballad, coral, echo, fable, onyx, nova, shimmer, verse

config:
  colorScheme: nord         # nord, tokyo-night, catppuccin, dracula
  tts:
    voice: noa              # voice preset name (from voices section)
    uiVoice: teo            # separate voice preset for UI narration
    speed: 1.2
    style: terrified        # TTS style/emotion instruction
    styleDegree: 2          # style intensity (0.01-2.0)
    localBackend: espeak    # termux (Android TTS), espeak (espeak-ng), none
    voiceRotation:          # cycle voice presets across agent tabs
      - alloy
      - ash
      - sage
      - noa
      - teo
    randomRotation: true    # random (true) vs sequential (false) assignment
    styleRotation:          # cycle styles across agent tabs
      - whispering
      - excited
      - friendly
      - terrified
  stt:
    model: whisper
    realtime: false
  session:
    cleanupTimeoutSeconds: 300
  ambient:                    # periodic status updates during agent silence
    enabled: false            # disabled by default; enable to get "still working" updates
    initialDelaySecs: 30      # first update after 30s of silence
    repeatIntervalSecs: 45    # subsequent updates every 45s
  healthMonitor:              # detect stuck/crashed agents
    enabled: true
    warningThresholdSecs: 300   # 5 min → warning
    unresponsiveThresholdSecs: 600  # 10 min → unresponsive
    checkIntervalSecs: 30     # how often to check
    checkTmuxPane: true       # verify tmux pane is alive
  notifications:              # webhook notifications (opt-in)
    enabled: false
    cooldownSecs: 60          # min gap between identical notifications
    channels:
      - name: phone
        type: ntfy            # ntfy, slack, discord, or webhook
        url: https://ntfy.sh/my-io-mcp
        priority: 3
        events: [health_warning, health_unresponsive, error]
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
    refresh: r
    dismiss: d                # dismiss active choice without responding

styles:                       # available TTS style/emotion names
  - whispering
  - excited
  - friendly
  - terrified
  # ... plus happy, calm, serious, neutral, etc.

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
| `d` | Dismiss active choice without responding (clears stale items) |
| `/` | Filter choices by typing |
| `t` | Spawn new Claude Code agent (local or remote) |
| `x` | Quick actions (configurable macros) |
| `c` | Toggle conversation mode (continuous voice chat) |
| `v` | Pane view (live tmux output for focused agent) |
| `i` | Freeform text input (wrapping, multi-line) |
| `m` | Queue message for agent (text) |
| `M` | Queue voice message (direct STT recording) |
| `space` | Voice input (toggle recording) |
| `s` | Settings menu |
| `p`/`P` | Replay prompt / all options |
| `r` | Refresh (config, tab bar, UI state) |
| `?` | Help (keyboard shortcut reference) |
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
uv run io-mcp        # Run directly (auto-starts proxy)
uv run io-mcp --dev  # Dev mode (uses uv run for proxy)
uv run io-mcp --restart  # Force kill all processes first
uv run io-mcp --default-config  # Ignore user config, use built-in defaults
uv run io-mcp --reset-config   # Delete config.yml and regenerate with defaults
io-mcp server        # Start proxy daemon only
io-mcp status        # Show health of proxy/backend/API
uv run pytest tests/ # Run tests
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

### NEVER Enter Plan Mode or Ask Questions

**NEVER use `EnterPlanMode` or `AskUserQuestion` tools.** The user interacts only via scroll wheel — they cannot type responses to questions or approve plans. These tools will block forever and make the app unresponsive. Instead:

- **Plan mode**: Just do the work directly. Use `speak_async()` to explain what you're about to do, then do it. Present choices via `present_choices()` if the user needs to make a decision.
- **Clarifying questions**: Present options via `present_choices()` instead of asking open-ended questions. The user can only select from a list.
- **All decisions** must go through `present_choices()` — never through text prompts.

### Register Your Session First

**Call `register_session()` as your first MCP tool call.** Provide your `cwd`, `hostname`, `tmux_session`, `tmux_pane`, and optionally a `name`, `voice`, and `emotion`. This lets io-mcp display your info in the dashboard and control your session (restart, send messages via tmux).

```
register_session(
  cwd="/path/to/project",
  hostname="my-machine",
  tmux_session="main",
  tmux_pane="%42",
  name="Code Review"
)
```

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
- Local `.io-mcp.local.yml` in cwd overrides `.io-mcp.yml` (gitignored, for personal overrides)
- Per-session voice/emotion rotation: set `voiceRotation`/`styleRotation` lists in config. Voice rotation uses preset names from the top-level `voices` section
- Key bindings are configurable in `config.keyBindings`
- Use `rename_session()` on connect to set a descriptive tab name
- Use `run_command()` to execute shell commands on the server device with user approval
- Use `present_multi_select()` for checkable batch selections
- User messages queued via `m` key appear in your next tool response — check for them
- Use `check_inbox()` to poll for queued messages during long operations without a tool call
- Queued messages are in-memory only (lost on backend restart)
- **Sessions are stateless**: no disk persistence. When the backend restarts, agents re-register fresh. This avoids ghost sessions and stale state.
- **UI voice**: `tts.uiVoice` config uses a separate voice for UI narration (settings, prompts, navigation) while agent speech uses the regular voice
- **Waiting view**: When the agent is working without choices, the right pane shows a clean waiting state with agent status and essential keyboard shortcut hints (m, s, d, v). The inbox list (left pane) remains visible for browsing history
- **Restart loop**: TUI runs inside a restart loop — "Restart TUI" cleanly exits and re-launches, "Quit" exits fully
- MCP server auto-restarts up to 5 times on crash (watchdog with exponential backoff)
- All 20 MCP tools wrapped with error safety — single tool errors don't crash the server
- Config validated on load with specific warnings for invalid references
- Ambient mode: escalating TTS updates during silence (30s initial, then every 45s with context). **Disabled by default.** Enable in `config.ambient.enabled: true`
- Agent activity indicator shows last speech in TUI
- **Health monitoring**: agents are checked every 30s for stuck/crashed state. Warning at 5 min, unresponsive at 10 min. Tmux pane liveness verified. Tab bar shows ⚠/✗ indicators. Configurable in `config.healthMonitor`
- **Notification webhooks**: send alerts to ntfy, Slack, Discord, or generic webhooks. Events: `health_warning`, `health_unresponsive`, `agent_connected`, `agent_disconnected`, `error`, `pulse_down`, `pulse_recovered`. Per-event cooldown prevents spam. Configure in `config.notifications`
- **Smart summaries**: sessions track tool call counts and build activity summaries. Dashboard shows per-agent summaries. Agent log (`g` key) shows unified timeline of speech + selections
- **Tab bar**: always visible — shows "io-mcp" branding when idle, agent name for single session, full tab bar for multiple agents. Health indicators shown per-tab
- **Dashboard actions**: selecting a session in the dashboard shows a sub-menu: Switch to, Close tab, Kill tmux pane, Back. Close tab is also available in the extra options menu
- **Dead session pruning**: health monitor auto-removes sessions with confirmed-dead tmux panes that are unresponsive. More aggressive than the standard 5-min stale timeout
- **TUI restart resilience**: backend uses a mutable app reference so tool dispatch survives TUI restarts. Pending `present_choices` calls automatically retry with the new TUI instance
- **Speech reminders**: tool responses include a reminder if the agent hasn't called `speak_async()` in over 60 seconds, nudging agents to narrate during long operations
- **Thinking phrases**: ambient updates use playful filler phrases ("Hmm, let me see", "One moment", "Huh, interesting") instead of generic status messages
- **Scroll readout TTS**: on scroll, plays cached API audio if available; otherwise falls back to `speak_async` (API generates + plays asynchronously). Local engines (termux-tts-speak, espeak) are only used in `--local` mode. espeak is NEVER used in non-local (API) mode.
- **Sequential TTS model**: speech never self-interrupts. A `_speech_lock` in the TTS engine ensures only one speech plays at a time. `speak()` and `speak_async()` queue behind current speech. Only user scroll/select (via `speak_with_local_fallback`) can interrupt — it stops current speech and plays immediately. No local TTS fallback in API mode — errors show visually in the TUI status line instead.
- **MCP abort handling**: when Claude Code cancels a tool call, the proxy sends a cancel request to `/cancel-mcp` on the backend. The backend resolves the pending inbox item with `_cancelled`, cleaning up the TUI. This prevents orphaned "waiting for selection" states.
- **Persistent UI state**: toggle states (e.g. inbox sidebar collapsed) are stored in `~/.config/io-mcp/state.json` and survive restarts. Use `state.get()`, `state.set()`, `state.toggle()` from `io_mcp.state`.
- **Auto-advance**: after selecting a choice, the TUI auto-switches to the next session with pending choices. No need to press `n` manually.
- **Number keys everywhere**: `1`-`9` number selection works in all menus: choices, settings, dashboard, dialogs, spawn menu, tab picker, quick actions, and setting value pickers
- **Hostname auto-detection**: server detects Tailscale DNS hostname (e.g. `harrys-macbook-pro`) from `tailscale status --json`. Overrides `localhost`, `.local`, or empty hostnames from agents. Only caches good values — retries Tailscale if it initially fails.
- **Two-column inbox layout**: when an agent sends choices, the TUI shows a left pane (inbox list of pending/completed items with status icons ●/○/✓) and a right pane (choices for the active item). Left pane is ~30% width. Items show truncated preambles and counts
- **Collapsed extras menu**: extra options are split into primary (always visible, e.g. "Record response") and secondary (hidden behind a "More options ›" toggle). Selecting the toggle expands/collapses the secondary extras. Reduces clutter in the default choices view
- **PulseAudio health check**: TTS engine adds a brief 50ms pause before PulseAudio playback to let the audio subsystem settle, preventing playback glitches on network audio
- **PulseAudio auto-recovery**: when PulseAudio goes down, the TUI attempts escalating recovery — kill orphaned paplay, restart daemon, suspend/resume sinks, full kill+restart. Sends `pulse_down` / `pulse_recovered` notifications. After exhausting attempts, speaks specific recovery steps and sends a high-priority alert. Attempts auto-reset after a backoff period (5× cooldown) so recovery keeps retrying. Configure in `config.pulseAudio`
- **`get_logs` MCP tool**: agents can call `get_logs(lines=50)` to retrieve recent TUI error logs, proxy logs, and speech history for debugging. Reads from `/tmp/io-mcp-tui-error.log` and `/tmp/io-mcp-proxy.log`
- **`--default-config` flag**: run `io-mcp --default-config` to ignore user config files and use built-in defaults only. Does not overwrite the config file on disk. Useful for debugging config issues
- **`--reset-config` flag**: run `io-mcp --reset-config` to delete `~/.config/io-mcp/config.yml` and regenerate it with all current defaults. Use this when your config has stale keys or missing new defaults. The old config is deleted and a fresh one is written with the latest defaults. You can also manually delete the config file and restart io-mcp to achieve the same effect

### Fallback TTS (when MCP is down)

If the io-mcp MCP server is unavailable (connection refused, tools not found), you can still speak to the user by piping TTS audio directly to their phone's PulseAudio server over Tailscale:

```bash
PULSE_SERVER=100.67.137.9 tts "Your message here" \
  --model gpt-4o-mini-tts --voice sage --speed 1.3 \
  --stdout --response-format wav \
  | PULSE_SERVER=100.67.137.9 paplay
```

Use the same host as the io-mcp MCP server (the phone's Tailscale IP). This bypasses MCP entirely and sends audio straight to the phone's speakers/earphones. Use this to:
- Narrate what you're doing when MCP tools are unavailable
- Communicate critical information if io-mcp crashes
- Provide a summary and ask the user to restart io-mcp
- Streaming TTS used automatically for blocking speak calls — lower latency
- Haptic feedback disabled by default; enable in `config.haptic.enabled: true`

## TUI Performance — Never Block the Event Loop

The TUI runs on Nix-on-Droid under **proot**, which intercepts every syscall. This has severe performance implications:

- **`subprocess.Popen` takes 200-300ms** (vs ~1ms on native Linux) due to fork/exec syscall interception
- **`os.killpg` / `proc.kill` take 10-50ms** per call
- **`os.path.isfile` / `stat` take 1-10ms** per call
- **`threading.Lock` acquisition** blocks if any holder is mid-syscall

The Textual event loop is single-threaded. **Any blocking call on the main thread freezes ALL input processing** — keys, scroll, timers, rendering. A 300ms Popen means 300ms of lost key events.

### Rules for the TUI event loop

1. **NEVER call `subprocess.Popen` on the main thread.** Always spawn in a background thread. This includes `termux-vibrate`, `termux-tts-speak`, `paplay`, `espeak`, `tts` CLI — any external process.

2. **NEVER call `self._tts.stop()` on the main thread.** `stop()` acquires `self._lock` and calls `os.killpg`, both of which can block on proot. Use `threading.Thread(target=self._tts.stop, daemon=True).start()` instead.

3. **NEVER hold `self._lock` during a Popen.** Move `Popen()` outside the lock. Only hold the lock briefly to update `self._process` / `self._termux_proc` references.

4. **Use `preexec_fn=os.setsid` on all subprocesses** that need to be killable. Then use `os.killpg(os.getpgid(proc.pid), signal.SIGKILL)` to kill the entire process group. Without this, wrapper scripts (like `termux-exec → termux-tts-speak`) leave orphaned child processes.

5. **Use generation counters for scroll TTS.** Increment a counter on each scroll event. Background threads check the counter before playing — if it's stale (user scrolled past), skip the audio. This prevents overlapping TTS from rapid scrolling.

6. **Don't use local TTS engines (termux-tts-speak, espeak) for scroll readout** unless in `--local` mode. Each call spawns a subprocess (200-300ms on proot). Instead, use the API TTS path: cache hit → instant playback, cache miss → `speak_async` (generates in background). First scroll through uncached options may be silent, but subsequent visits play cached audio instantly.

7. **Rate-limit thread creation.** Each `threading.Thread().start()` is cheap on native Linux but adds up on proot. Avoid spawning threads in tight loops (e.g., one per scroll event for vibration + TTS + API generation = 3 threads per scroll).

8. **Keep `on_highlight_changed` fast.** This fires on every scroll/key event. It should only do: trivial dict lookups, increment a counter, and spawn at most one background thread. No filesystem access, no lock acquisition, no subprocess calls.

### TTS speech model

```
speak() / speak_async():
  Acquire _speech_lock → wait for current speech → play → release
  Sequential — no self-interruption, no overlap

speak_with_local_fallback() (scroll/select overlay):
  stop_sync() → kill current speech → play immediately
  User-triggered — CAN interrupt current speech

Pregeneration:
  Sequential _generate_to_file() in background thread
  No speech lock needed — just generates WAV files to cache
```

Agent speech (`speak`, `speak_async`) always uses the full API TTS pipeline. UI speech (`_speak_ui`) uses `speak_async` with optional `uiVoice` override.

### Common pitfalls

| Symptom | Cause | Fix |
|---------|-------|-----|
| Keys ignored on single tap but work when held | Main thread blocked 200-300ms; first keydown lost, key repeat events arrive after block clears | Move blocking call to background thread |
| TTS overlapping on rapid scroll | Multiple background threads all call `_start_playback` | Use `_scroll_gen` counter; check before playing |
| Zombie `termux-tts-speak` processes | `proc.kill()` only kills wrapper script, not child | Use `preexec_fn=os.setsid` + `os.killpg` |
| Lock contention on `self._lock` | `stop()` and `_start_playback` both acquire lock; Popen inside lock | Move Popen outside lock; keep lock hold time < 1ms |
| Thread pile-up during rapid scroll | Each scroll spawns 2-3 threads; threads block on Popen/lock | Reduce threads per scroll; use fire-and-forget |
