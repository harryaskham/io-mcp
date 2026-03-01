"""Configuration system for io-mcp.

Reads/writes config from $HOME/.config/io-mcp/config.yml (or --config-file).
Also merges with a local .io-mcp.yml if found in the current directory
(local takes precedence over global config), and .io-mcp.local.yml on top
of that for personal/gitignored overrides.

Config strings can include shell variables like ${OPENAI_API_KEY} which are
expanded at load time.

The config defines:
  - providers: named API endpoints with baseUrl and apiKey
  - voices: named voice presets mapping friendly names to provider/model/voice
  - models: stt and realtime model definitions with provider associations
  - config: runtime settings (selected voice preset, speed, etc.)
  - extraOptions: options appended to every choice list (with optional silent flag)
"""

from __future__ import annotations

import copy
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml


# ─── Djent integration constants ──────────────────────────────────────────

_DJENT_EXTRA_OPTIONS: list[dict[str, Any]] = [
    {"title": "Djent status", "description": "Show current djent project status", "silent": True},
    {"title": "Djent dashboard", "description": "Open the djent TUI dashboard", "silent": True},
    {"title": "New task", "description": "Create a new backlog task", "silent": True},
    {"title": "Start dev loop", "description": "Run (loop/dev) to implement next bead", "silent": True},
    {"title": "Stop swarm", "description": "Gracefully stop all djent agents", "silent": True},
]

_DJENT_QUICK_ACTIONS: list[dict[str, Any]] = [
    {"key": "!", "label": "Djent status", "action": "command", "value": "djent status 2>&1 | head -40"},
    {"key": "@", "label": "List agents", "action": "command", "value": "djent agents -n 10 2>&1"},
    {"key": "#", "label": "Backlog", "action": "command", "value": "bd list 2>&1 | head -30"},
    {"key": "$", "label": "Start dev loop", "action": "command",
     "value": "tmux new-window -n djent 'djent -e \"(loop/dev)\"'"},
    {"key": "%", "label": "Review PRs", "action": "command",
     "value": "tmux new-window -n review 'djent -e \"(loop/review)\"'"},
    {"key": "^", "label": "Stop swarm", "action": "command", "value": "djent down 2>&1"},
    {"key": "&", "label": "Tail logs", "action": "command", "value": "djent log 2>&1 | tail -20"},
    {"key": "*", "label": "Djent dashboard", "action": "command",
     "value": "tmux new-window -n dash 'djent dash'"},
]


DEFAULT_CONFIG_DIR = os.path.join(
    os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")),
    "io-mcp",
)
DEFAULT_CONFIG_FILE = os.path.join(DEFAULT_CONFIG_DIR, "config.yml")

# Full default config — written on first run, used as fallback for missing keys
DEFAULT_CONFIG: dict[str, Any] = {
    "providers": {
        "openai": {
            "baseUrl": "${OPENAI_BASE_URL:-https://api.openai.com}",
            "apiKey": "${OPENAI_API_KEY}",
        },
        "azure-foundry": {
            "baseUrl": "${AZURE_WCUS_ENDPOINT:-https://harryaskham-sandbox-ais-wcus.services.ai.azure.com}",
            "apiKey": "${AZURE_WCUS_API_KEY}",
        },
        "azure-speech": {
            "baseUrl": "${AZURE_SPEECH_ENDPOINT:-https://eastus.tts.speech.microsoft.com}",
            "apiKey": "${AZURE_SPEECH_API_KEY}",
        },
    },
    "models": {
        "realtime": {
            "gpt-realtime": {
                "provider": "openai",
            },
        },
        "stt": {
            "gpt-4o-mini-transcribe": {
                "provider": "openai",
                "supportsRealtime": True,
            },
            "whisper": {
                "provider": "openai",
                "supportsRealtime": True,
            },
            "mai-ears-1": {
                "provider": "azure-foundry",
                "supportsRealtime": False,
            },
        },
    },
    # Named voice presets — each maps a friendly name to provider/model/voice.
    # Use these names everywhere: config.tts.voice, voiceRotation, uiVoice.
    "voices": {
        "alloy": {"provider": "openai", "model": "gpt-4o-mini-tts", "voice": "alloy"},
        "ash": {"provider": "openai", "model": "gpt-4o-mini-tts", "voice": "ash"},
        "ballad": {"provider": "openai", "model": "gpt-4o-mini-tts", "voice": "ballad"},
        "coral": {"provider": "openai", "model": "gpt-4o-mini-tts", "voice": "coral"},
        "echo": {"provider": "openai", "model": "gpt-4o-mini-tts", "voice": "echo"},
        "fable": {"provider": "openai", "model": "gpt-4o-mini-tts", "voice": "fable"},
        "onyx": {"provider": "openai", "model": "gpt-4o-mini-tts", "voice": "onyx"},
        "nova": {"provider": "openai", "model": "gpt-4o-mini-tts", "voice": "nova"},
        "sage": {"provider": "openai", "model": "gpt-4o-mini-tts", "voice": "sage"},
        "shimmer": {"provider": "openai", "model": "gpt-4o-mini-tts", "voice": "shimmer"},
        "verse": {"provider": "openai", "model": "gpt-4o-mini-tts", "voice": "verse"},
        "noa": {"provider": "openai", "model": "azure/speech/azure-tts", "voice": "en-US-Noa:MAI-Voice-1"},
        "teo": {"provider": "openai", "model": "azure/speech/azure-tts", "voice": "en-US-Teo:MAI-Voice-1"},
    },
    "config": {
        "colorScheme": "nord",
        "realtime": {
            "model": "gpt-realtime",
        },
        "tts": {
            "voice": "noa",
            "uiVoice": "teo",
            "speed": 1.0,
            "speeds": {
                # Per-context speed multipliers — applied on top of the base speed.
                # Final speed = base_speed × multiplier. E.g. base 1.2 × scroll 1.3 = 1.56.
                "scroll": 1.3,         # scroll readout — faster for quick scanning
                "speak": 1.5,          # blocking agent speech (speak())
                "speakAsync": 2.0,     # non-blocking agent speech (speak_async())
                "preamble": 1.0,       # preamble readout when choices arrive
                "agent": 1.0,          # generic agent speech (fallback for speak/speakAsync)
                "choiceLabel": 1.5,    # choice option labels during readout
                "choiceSummary": 2.0,  # choice option summaries during readout
                "ui": 1.5,            # UI narration (settings, menus, numbers, "selected")
            },
            "style": "whispering",
            "styleDegree": 2,
            "localBackend": "espeak",  # "termux", "espeak", or "none"
            "pregenerateWorkers": 3,   # concurrent TTS processes for pregeneration (1-8)
            "voiceRotation": [
                "noa", "teo",
            ],
            "randomRotation": True,  # random (True) vs sequential (False) voice/emotion assignment
            "styleRotation": [
                "angry", "chat", "cheerful", "excited", "friendly",
                "hopeful", "sad", "shouting", "terrified", "unfriendly", "whispering",
            ],
        },
        "stt": {
            "model": "whisper",
            "realtime": False,
        },
        "session": {
            "cleanupTimeoutSeconds": 300,
        },
        "ambient": {
            "enabled": False,
            "initialDelaySecs": 30,
            "repeatIntervalSecs": 45,
        },
        "pulseAudio": {
            "autoReconnect": True,             # attempt auto-reconnect when PulseAudio goes down
            "maxReconnectAttempts": 3,          # max consecutive reconnect attempts before giving up
            "reconnectCooldownSecs": 30,       # min seconds between reconnect attempts
        },
        "scroll": {
            "debounce": 0.15,                  # minimum seconds between scroll events
            "invert": False,                   # reverse scroll direction (for rings that spin the "wrong" way)
        },
        "scrollAcceleration": {
            "enabled": True,                   # detect rapid scrolling and skip items
            "fastThresholdMs": 80,             # avg interval below this → skip fastSkip items
            "turboThresholdMs": 40,            # avg interval below this → skip turboSkip items
            "fastSkip": 3,                     # items to skip in fast mode
            "turboSkip": 5,                    # items to skip in turbo mode
        },
        "conversation": {
            "autoReply": False,                # auto-select "continue" choices in conversation mode
            "autoReplyDelaySecs": 3.0,         # delay before auto-selecting
        },
        "dwell": {
            "enabled": False,                  # master toggle for dwell-to-select
            "durationSeconds": 3.0,            # seconds to dwell before auto-selecting
        },
        "haptic": {
            "enabled": False,                  # disabled by default; enable on Android/Termux
        },
        "chimes": {
            "enabled": False,                  # disabled by default; enable for audio cues
        },
        "healthMonitor": {
            "enabled": True,
            "warningThresholdSecs": 300,      # 5 minutes with no tool call → warning
            "unresponsiveThresholdSecs": 600,  # 10 minutes with no tool call → unresponsive
            "checkIntervalSecs": 30,           # how often to run the health check
            "checkTmuxPane": True,             # verify tmux pane is still alive
        },
        "notifications": {
            "enabled": False,                  # opt-in: must configure channels
            "cooldownSecs": 60,                # min gap between identical notifications
            "channels": [],                    # list of {name, type, url, events, ...}
        },
        "agents": {
            "defaultWorkdir": "~",
            "hosts": [],
        },
        "keyBindings": {
            "cursorDown": "j",
            "cursorUp": "k",
            "select": "enter",
            "voiceInput": "space",
            "freeformInput": "i",
            "queueMessage": "m",
            "settings": "s",
            "replayPrompt": "p",
            "replayAll": "P",
            "nextTab": "l",
            "prevTab": "h",
            "nextChoicesTab": "n",
            "undoSelection": "u",
            "filterChoices": "slash",
            "spawnAgent": "t",
            "multiSelect": "x",
            "conversationMode": "c",
            "paneView": "v",
            "toggleSidebar": "b",
            "hotReload": "r",
            "quit": "q",
        },
        "djent": {
            "enabled": False,
        },
        "alwaysAllow": {
            "restartTUI": True,             # skip confirmation dialog for TUI restart
        },
    },
    "styles": [
        "angry", "chat", "cheerful", "excited", "friendly",
        "hopeful", "sad", "shouting", "terrified", "unfriendly", "whispering",
    ],
}


def _expand_env(value: str) -> str:
    """Expand shell-style ${VAR} and ${VAR:-default} in a string."""
    def _replacer(m: re.Match) -> str:
        var_expr = m.group(1)
        if ":-" in var_expr:
            var_name, default = var_expr.split(":-", 1)
            return os.environ.get(var_name, default)
        return os.environ.get(var_expr, "")
    return re.sub(r"\$\{([^}]+)\}", _replacer, value)


def _expand_config(obj: Any) -> Any:
    """Recursively expand env vars in all string values."""
    if isinstance(obj, str):
        return _expand_env(obj)
    elif isinstance(obj, dict):
        return {k: _expand_config(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_expand_config(v) for v in obj]
    return obj


def _deep_merge(base: dict, override: dict) -> dict:
    """Deep-merge override into base. Override values win."""
    result = dict(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _closest_match(key: str, valid_keys: set[str], max_distance: int = 3) -> str | None:
    """Find the closest match for a key in a set of valid keys.

    Uses Levenshtein-style edit distance to suggest typo corrections.
    Returns None if no match is close enough (within max_distance edits).
    """
    best_match = None
    best_dist = max_distance + 1
    key_lower = key.lower()
    for candidate in valid_keys:
        cand_lower = candidate.lower()
        if key_lower == cand_lower:
            return candidate
        # Quick length check — if lengths differ too much, skip
        if abs(len(key_lower) - len(cand_lower)) > max_distance:
            continue
        # Simple edit distance (Levenshtein)
        dist = _edit_distance(key_lower, cand_lower)
        if dist < best_dist:
            best_dist = dist
            best_match = candidate
    return best_match if best_dist <= max_distance else None


def _edit_distance(a: str, b: str) -> int:
    """Compute Levenshtein edit distance between two strings."""
    if len(a) > len(b):
        a, b = b, a
    prev = list(range(len(a) + 1))
    for j in range(1, len(b) + 1):
        curr = [j] + [0] * len(a)
        for i in range(1, len(a) + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            curr[i] = min(curr[i - 1] + 1, prev[i] + 1, prev[i - 1] + cost)
        prev = curr
    return prev[len(a)]


def _find_new_keys(defaults: dict, user: dict, prefix: str = "") -> list[str]:
    """Find keys present in defaults but missing from user config.

    Returns a list of dotted key paths (e.g. "config.tts.localBackend")
    for keys that were added from defaults because the user's config
    didn't have them.
    """
    new_keys: list[str] = []
    for key, default_value in defaults.items():
        path = f"{prefix}.{key}" if prefix else key
        if key not in user:
            new_keys.append(path)
        elif isinstance(default_value, dict) and isinstance(user.get(key), dict):
            new_keys.extend(_find_new_keys(default_value, user[key], path))
    return new_keys


@dataclass
class IoMcpConfig:
    """Parsed and expanded io-mcp configuration."""

    raw: dict[str, Any] = field(default_factory=dict)
    """The raw config as loaded from YAML (with env vars unexpanded)."""

    expanded: dict[str, Any] = field(default_factory=dict)
    """The config with all env vars expanded."""

    config_path: str = DEFAULT_CONFIG_FILE
    """Path to the config file."""

    validation_warnings: list[str] = field(default_factory=list)
    """Warnings from the last validation run."""

    @classmethod
    def reset(cls, config_path: Optional[str] = None) -> "IoMcpConfig":
        """Delete the config file and regenerate it with all current defaults.

        This is useful when the config has stale keys or the user wants a
        clean slate with all the latest default values.

        Returns the freshly-created IoMcpConfig with defaults.
        """
        path = config_path or DEFAULT_CONFIG_FILE
        if os.path.isfile(path):
            os.unlink(path)
            print(f"  Config: deleted {path}", flush=True)
        # Load will see the file is missing and create it with defaults
        return cls.load(path)

    @classmethod
    def load(cls, config_path: Optional[str] = None) -> "IoMcpConfig":
        """Load config from file, creating with defaults if not found.

        Merge order (later takes precedence):
        1. DEFAULT_CONFIG (built-in defaults)
        2. ~/.config/io-mcp/config.yml (user config)
        3. .io-mcp.yml in cwd (project-local, checked into repo)
        4. .io-mcp.local.yml in cwd (personal overrides, gitignored)

        CLI flags override all of the above at runtime.
        """
        path = config_path or DEFAULT_CONFIG_FILE
        raw = copy.deepcopy(DEFAULT_CONFIG)

        new_keys: list[str] = []
        if os.path.isfile(path):
            try:
                with open(path, "r") as f:
                    user_config = yaml.safe_load(f)
                if user_config and isinstance(user_config, dict):
                    # Detect keys added from defaults before merging
                    new_keys = _find_new_keys(raw, user_config)
                    raw = _deep_merge(raw, user_config)
            except Exception as e:
                print(f"WARNING: Failed to load config from {path}: {e}", flush=True)
        else:
            # Create config file with defaults
            os.makedirs(os.path.dirname(path), exist_ok=True)
            try:
                with open(path, "w") as f:
                    yaml.dump(raw, f, default_flow_style=False, sort_keys=False)
                print(f"  Config: created {path}", flush=True)
            except Exception as e:
                print(f"WARNING: Failed to write default config to {path}: {e}", flush=True)

        # Merge local .io-mcp.yml (cwd takes precedence over user config)
        local_path = os.path.join(os.getcwd(), ".io-mcp.yml")
        if os.path.isfile(local_path):
            try:
                with open(local_path, "r") as f:
                    local_config = yaml.safe_load(f)
                if local_config and isinstance(local_config, dict):
                    raw = _deep_merge(raw, local_config)
                    print(f"  Config: merged local {local_path}", flush=True)
            except Exception as e:
                print(f"WARNING: Failed to load local config from {local_path}: {e}", flush=True)

        # Merge .io-mcp.local.yml (personal overrides, should be gitignored)
        local_override_path = os.path.join(os.getcwd(), ".io-mcp.local.yml")
        if os.path.isfile(local_override_path):
            try:
                with open(local_override_path, "r") as f:
                    local_override = yaml.safe_load(f)
                if local_override and isinstance(local_override, dict):
                    raw = _deep_merge(raw, local_override)
                    print(f"  Config: merged local override {local_override_path}", flush=True)
            except Exception as e:
                print(f"WARNING: Failed to load local override from {local_override_path}: {e}", flush=True)

        expanded = _expand_config(raw)
        cfg = cls(raw=raw, expanded=expanded, config_path=path)
        cfg._validate()

        # Log any new default keys that were added to the user's config
        # Filter out provider/model keys — only report config-level keys
        # that affect behavior and the user might want to customize.
        config_new_keys = [k for k in new_keys if not k.startswith(("providers.", "models."))]
        if config_new_keys:
            print(f"  Config: added {len(config_new_keys)} new default key(s) to {path}:", flush=True)
            for key in config_new_keys:
                print(f"    + {key}", flush=True)

        # Always write back the full config (defaults + user overrides)
        # so the user can see all available options in config.yml.
        # This is the auto-migration mechanism: new keys from DEFAULT_CONFIG
        # are merged into the user's file on every load/reload.
        try:
            cfg.save()
        except Exception:
            pass
        return cfg

    def _validate(self) -> None:
        """Validate config structure and report warnings for issues."""
        warnings: list[str] = []

        # ── Unknown top-level keys ────────────────────────────────
        known_top_level = {"providers", "voices", "models", "config",
                           "styles", "extraOptions", "quickActions"}
        for key in self.raw:
            if key not in known_top_level:
                # Suggest closest match for likely typos
                _suggest = _closest_match(key, known_top_level)
                hint = f" (did you mean '{_suggest}'?)" if _suggest else ""
                warnings.append(
                    f"Unknown top-level key '{key}'{hint} — "
                    f"expected one of: {', '.join(sorted(known_top_level))}"
                )

        # Check required top-level keys
        for key in ("providers", "voices", "config"):
            if key not in self.raw:
                warnings.append(f"Missing top-level key '{key}' — using defaults")

        voices = self.raw.get("voices", {})

        # ── Unknown keys inside config section ────────────────────
        known_config_keys = {
            "colorScheme", "tts", "stt", "realtime", "session",
            "ambient", "pulseAudio", "scroll", "scrollAcceleration",
            "conversation", "dwell", "haptic", "chimes", "healthMonitor",
            "notifications", "agents", "keyBindings", "djent",
            "alwaysAllow", "ringReceiver",
        }
        user_config = self.raw.get("config", {})
        if isinstance(user_config, dict):
            for key in user_config:
                if key not in known_config_keys:
                    _suggest = _closest_match(key, known_config_keys)
                    hint = f" (did you mean '{_suggest}'?)" if _suggest else ""
                    warnings.append(
                        f"Unknown config key 'config.{key}'{hint} — "
                        f"expected one of: {', '.join(sorted(known_config_keys))}"
                    )

        # ── Unknown keys inside config.tts ────────────────────────
        known_tts_keys = {
            "voice", "uiVoice", "speed", "speeds", "style", "emotion",
            "styleDegree", "localBackend", "pregenerateWorkers",
            "voiceRotation", "randomRotation", "styleRotation",
            "emotionRotation",
        }
        user_tts = user_config.get("tts", {}) if isinstance(user_config, dict) else {}
        if isinstance(user_tts, dict):
            for key in user_tts:
                if key not in known_tts_keys:
                    _suggest = _closest_match(key, known_tts_keys)
                    hint = f" (did you mean '{_suggest}'?)" if _suggest else ""
                    warnings.append(
                        f"Unknown TTS key 'config.tts.{key}'{hint} — "
                        f"expected one of: {', '.join(sorted(known_tts_keys))}"
                    )

        # ── Unknown keys inside config.tts.speeds ─────────────────
        known_speed_contexts = {
            "speak", "speakAsync", "preamble",
            "choiceLabel", "choiceSummary", "ui",
            "scroll", "agent",
        }
        user_speeds = user_tts.get("speeds", {}) if isinstance(user_tts, dict) else {}
        if isinstance(user_speeds, dict):
            for key in user_speeds:
                if key not in known_speed_contexts:
                    _suggest = _closest_match(key, known_speed_contexts)
                    hint = f" (did you mean '{_suggest}'?)" if _suggest else ""
                    warnings.append(
                        f"Unknown speed context 'config.tts.speeds.{key}'{hint} — "
                        f"expected one of: {', '.join(sorted(known_speed_contexts))}"
                    )

        # Check TTS voice preset exists
        tts_voice = self.runtime.get("tts", {}).get("voice", "")
        if tts_voice and tts_voice not in voices:
            warnings.append(f"TTS voice preset '{tts_voice}' not found in voices — available: {list(voices.keys())}")

        # Check UI voice preset exists
        ui_voice = self.runtime.get("tts", {}).get("uiVoice", "")
        if ui_voice and ui_voice not in voices:
            warnings.append(f"UI voice preset '{ui_voice}' not found in voices — available: {list(voices.keys())}")

        # Check voice rotation presets exist
        voice_rot = self.runtime.get("tts", {}).get("voiceRotation", [])
        for entry in voice_rot:
            name = entry if isinstance(entry, str) else entry.get("voice", "") if isinstance(entry, dict) else ""
            if name and name not in voices:
                warnings.append(f"Voice rotation entry '{name}' not found in voices — available: {list(voices.keys())}")

        # Check providers referenced by voice presets exist
        for name, vdef in voices.items():
            provider = vdef.get("provider", "") if isinstance(vdef, dict) else ""
            if provider and provider not in self.raw.get("providers", {}):
                warnings.append(f"Voice preset '{name}' references provider '{provider}' which is not defined")

        # Check STT model exists in models
        stt_model = self.runtime.get("stt", {}).get("model", "")
        stt_models = self.raw.get("models", {}).get("stt", {})
        if stt_model and stt_model not in stt_models:
            warnings.append(f"STT model '{stt_model}' not found in models.stt — available: {list(stt_models.keys())}")

        # Check providers referenced by STT models exist
        for name, model_def in self.raw.get("models", {}).get("stt", {}).items():
            provider = model_def.get("provider", "")
            if provider and provider not in self.raw.get("providers", {}):
                warnings.append(f"STT model '{name}' references provider '{provider}' which is not defined")

        # ── Style/emotion validation ──────────────────────────────
        style = self.runtime.get("tts", {}).get("style",
                self.runtime.get("tts", {}).get("emotion", ""))
        styles = self.expanded.get("styles", [])
        if style and styles and style not in styles:
            # Not a hard error — custom style names are allowed — but warn
            warnings.append(
                f"TTS style '{style}' not in styles list "
                f"(custom styles are OK, but check for typos) — "
                f"known styles: {styles}"
            )

        # ── Style rotation validation ─────────────────────────────
        style_rot = self.runtime.get("tts", {}).get("styleRotation",
                    self.runtime.get("tts", {}).get("emotionRotation", []))
        if style_rot and styles:
            for entry in style_rot:
                if isinstance(entry, str) and entry not in styles:
                    warnings.append(
                        f"Style rotation entry '{entry}' not in styles list "
                        f"(custom styles are OK, but check for typos)"
                    )

        # Check speed is in valid range
        speed = self.runtime.get("tts", {}).get("speed", 1.0)
        if not isinstance(speed, (int, float)) or speed < 0.1 or speed > 5.0:
            warnings.append(f"TTS speed {speed} is out of range (0.1-5.0)")

        # ── Per-context speed validation ──────────────────────────
        speeds_cfg = self.runtime.get("tts", {}).get("speeds", {})
        if isinstance(speeds_cfg, dict):
            for ctx, val in speeds_cfg.items():
                if ctx in known_speed_contexts:
                    if not isinstance(val, (int, float)):
                        warnings.append(f"config.tts.speeds.{ctx} must be a number, got {type(val).__name__}")
                    elif val < 0.1 or val > 5.0:
                        warnings.append(f"config.tts.speeds.{ctx} ({val}) is out of range (0.1-5.0)")

        # ── localBackend validation ───────────────────────────────
        local_backend = self.runtime.get("tts", {}).get("localBackend", "espeak")
        valid_backends = {"termux", "espeak", "none"}
        if local_backend not in valid_backends:
            warnings.append(
                f"config.tts.localBackend '{local_backend}' is not valid — "
                f"expected one of: {', '.join(sorted(valid_backends))}"
            )

        # ── colorScheme validation ────────────────────────────────
        color_scheme = self.runtime.get("colorScheme", "nord")
        valid_schemes = {"nord", "tokyo-night", "catppuccin", "dracula"}
        if color_scheme not in valid_schemes:
            warnings.append(
                f"config.colorScheme '{color_scheme}' is not valid — "
                f"expected one of: {', '.join(sorted(valid_schemes))}"
            )

        # ── pregenerateWorkers range warning ──────────────────────
        pregen = self.runtime.get("tts", {}).get("pregenerateWorkers", 3)
        if isinstance(pregen, (int, float)):
            if pregen < 1 or pregen > 8:
                warnings.append(
                    f"config.tts.pregenerateWorkers ({pregen}) is out of range — "
                    f"will be clamped to 1-8"
                )

        # ── styleDegree range validation ──────────────────────────
        style_degree = self.runtime.get("tts", {}).get("styleDegree")
        if style_degree is not None:
            if not isinstance(style_degree, (int, float)):
                warnings.append(f"config.tts.styleDegree must be a number, got {type(style_degree).__name__}")
            elif style_degree < 0.01 or style_degree > 2.0:
                warnings.append(
                    f"config.tts.styleDegree ({style_degree}) is out of range (0.01-2.0)"
                )

        # ── Key bindings validation ───────────────────────────────
        known_actions = set(DEFAULT_CONFIG.get("config", {}).get("keyBindings", {}).keys())
        user_bindings = user_config.get("keyBindings", {}) if isinstance(user_config, dict) else {}
        if isinstance(user_bindings, dict):
            for action in user_bindings:
                if action not in known_actions:
                    _suggest = _closest_match(action, known_actions)
                    hint = f" (did you mean '{_suggest}'?)" if _suggest else ""
                    warnings.append(
                        f"Unknown key binding action 'config.keyBindings.{action}'{hint} — "
                        f"expected one of: {', '.join(sorted(known_actions))}"
                    )

        # ── Health monitor validation ─────────────────────────────
        health_cfg = self.runtime.get("healthMonitor", {})
        if health_cfg:
            warn_thresh = health_cfg.get("warningThresholdSecs", 300)
            unresp_thresh = health_cfg.get("unresponsiveThresholdSecs", 600)
            check_interval = health_cfg.get("checkIntervalSecs", 30)

            if isinstance(warn_thresh, (int, float)) and isinstance(unresp_thresh, (int, float)):
                if warn_thresh >= unresp_thresh:
                    warnings.append(
                        f"healthMonitor.warningThresholdSecs ({warn_thresh}) >= "
                        f"unresponsiveThresholdSecs ({unresp_thresh}) — "
                        "warning should be shorter than unresponsive"
                    )
            if isinstance(check_interval, (int, float)) and check_interval < 5:
                warnings.append(
                    f"healthMonitor.checkIntervalSecs ({check_interval}) is very low — "
                    "consider >= 10 seconds to avoid excessive checking"
                )

        # ── Notification channel validation ───────────────────────
        notif_cfg = self.runtime.get("notifications", {})
        if notif_cfg.get("enabled", False):
            channels = notif_cfg.get("channels", [])
            if not channels:
                warnings.append("notifications.enabled is true but no channels configured")
            for i, ch in enumerate(channels):
                ch_name = ch.get("name", f"channel[{i}]")
                ch_url = ch.get("url", "")
                ch_type = ch.get("type", "webhook")

                if not ch_url:
                    warnings.append(f"Notification channel '{ch_name}' has no URL")
                elif not ch_url.startswith(("http://", "https://")):
                    warnings.append(
                        f"Notification channel '{ch_name}' URL doesn't start with http(s):// — "
                        f"got: {ch_url[:50]}"
                    )

                valid_types = {"ntfy", "slack", "discord", "webhook"}
                if ch_type not in valid_types:
                    warnings.append(
                        f"Notification channel '{ch_name}' has unknown type '{ch_type}' — "
                        f"expected one of: {', '.join(sorted(valid_types))}"
                    )

                events = ch.get("events", [])
                if events:
                    valid_events = {"all", "health_warning", "health_unresponsive",
                                    "choices_timeout", "agent_connected",
                                    "agent_disconnected", "error",
                                    "pulse_down", "pulse_recovered"}
                    for evt in events:
                        if evt not in valid_events:
                            warnings.append(
                                f"Notification channel '{ch_name}' has unknown event '{evt}' — "
                                f"expected one of: {', '.join(sorted(valid_events))}"
                            )

            cooldown = notif_cfg.get("cooldownSecs", 60)
            if isinstance(cooldown, (int, float)) and cooldown < 0:
                warnings.append(f"notifications.cooldownSecs ({cooldown}) cannot be negative")

        # ── Voice preset structure validation ─────────────────────
        for name, vdef in voices.items():
            if not isinstance(vdef, dict):
                warnings.append(
                    f"Voice preset '{name}' should be a dict with "
                    f"provider/model/voice keys, got {type(vdef).__name__}"
                )
            else:
                missing = [k for k in ("provider", "model", "voice") if k not in vdef]
                if missing:
                    warnings.append(
                        f"Voice preset '{name}' is missing keys: {', '.join(missing)}"
                    )

        # ── Store warnings for programmatic access ────────────────
        self.validation_warnings = list(warnings)

        # Report warnings
        for w in warnings:
            print(f"  Config WARNING: {w}", flush=True)

    def save(self) -> None:
        """Write the raw config back to disk."""
        os.makedirs(os.path.dirname(self.config_path), exist_ok=True)
        with open(self.config_path, "w") as f:
            yaml.dump(self.raw, f, default_flow_style=False, sort_keys=False)
        # Re-expand after save
        self.expanded = _expand_config(self.raw)

    def reload(self) -> None:
        """Reload from disk."""
        fresh = IoMcpConfig.load(self.config_path)
        self.raw = fresh.raw
        self.expanded = fresh.expanded

    # ─── Accessors ──────────────────────────────────────────────────

    @property
    def providers(self) -> dict[str, Any]:
        return self.expanded.get("providers", {})

    @property
    def models(self) -> dict[str, Any]:
        return self.expanded.get("models", {})

    @property
    def runtime(self) -> dict[str, Any]:
        return self.expanded.get("config", {})

    # ─── Voice presets ────────────────────────────────────────────

    @property
    def voices(self) -> dict[str, Any]:
        """Named voice presets: {name: {provider, model, voice}}."""
        return self.expanded.get("voices", {})

    def resolve_voice(self, name: str) -> dict[str, Any]:
        """Resolve a voice preset name to its full definition.

        Returns dict with keys: provider, model, voice, base_url, api_key.
        Falls back to treating the name as a raw voice string on the
        current default provider if no preset matches.
        """
        preset = self.voices.get(name, {})
        if preset:
            provider_name = preset.get("provider", "openai")
            provider_def = self.providers.get(provider_name, {})
            return {
                "provider": provider_name,
                "model": preset.get("model", "gpt-4o-mini-tts"),
                "voice": preset.get("voice", name),
                "base_url": provider_def.get("baseUrl", "https://api.openai.com"),
                "api_key": provider_def.get("apiKey", ""),
            }
        # Fallback: treat name as raw voice string, use openai defaults
        provider_def = self.providers.get("openai", {})
        return {
            "provider": "openai",
            "model": "gpt-4o-mini-tts",
            "voice": name,
            "base_url": provider_def.get("baseUrl", "https://api.openai.com"),
            "api_key": provider_def.get("apiKey", ""),
        }

    @property
    def voice_preset_names(self) -> list[str]:
        """All available voice preset names."""
        return list(self.voices.keys())

    # ─── TTS accessors ──────────────────────────────────────────────

    @property
    def tts_model_name(self) -> str:
        """The TTS model from the active voice preset."""
        return self.resolve_voice(self.tts_voice_preset).get("model", "gpt-4o-mini-tts")

    @property
    def tts_model_def(self) -> dict[str, Any]:
        """Full resolved definition for the active voice preset."""
        return self.resolve_voice(self.tts_voice_preset)

    @property
    def tts_provider_name(self) -> str:
        return self.resolve_voice(self.tts_voice_preset).get("provider", "openai")

    @property
    def tts_provider(self) -> dict[str, Any]:
        return self.providers.get(self.tts_provider_name, {})

    @property
    def tts_base_url(self) -> str:
        return self.resolve_voice(self.tts_voice_preset).get("base_url", "https://api.openai.com")

    @property
    def tts_api_key(self) -> str:
        return self.resolve_voice(self.tts_voice_preset).get("api_key", "")

    @property
    def tts_voice_preset(self) -> str:
        """The current voice preset name (e.g. 'sage', 'noa')."""
        return self.runtime.get("tts", {}).get("voice", "sage")

    @property
    def tts_voice(self) -> str:
        """The raw voice string for the TTS CLI (e.g. 'sage', 'en-US-Noa:MAI-Voice-1')."""
        return self.resolve_voice(self.tts_voice_preset).get("voice", "sage")

    @property
    def tts_ui_voice_preset(self) -> str:
        """Get the UI voice preset name. Falls back to regular voice if not set."""
        ui = self.runtime.get("tts", {}).get("uiVoice", "")
        return ui if ui else self.tts_voice_preset

    @property
    def tts_ui_voice(self) -> str:
        """Get the UI voice raw string. Falls back to regular voice if not set."""
        return self.resolve_voice(self.tts_ui_voice_preset).get("voice", self.tts_voice)

    @property
    def tts_speed(self) -> float:
        return float(self.runtime.get("tts", {}).get("speed", 1.0))

    def tts_speed_for(self, context: str) -> float:
        """Get TTS speed for a specific context.

        Values in config.tts.speeds are **multipliers** applied to the base
        speed (config.tts.speed).  For example, base speed 1.2 with a scroll
        multiplier of 1.3 yields 1.56.

        If the context is not found in speeds, falls back to the base speed
        (i.e. an implicit multiplier of 1.0).

        Contexts: scroll, speak, speakAsync, preamble, agent,
                  choiceLabel, choiceSummary, ui
        """
        base = self.tts_speed
        speeds = self.runtime.get("tts", {}).get("speeds", {})
        multiplier = speeds.get(context)
        if multiplier is not None:
            return round(base * float(multiplier), 4)
        return self.tts_speed

    @property
    def tts_emotion(self) -> str:
        # Legacy alias — reads "style" first, falls back to "emotion" for migration
        return self.runtime.get("tts", {}).get("style",
               self.runtime.get("tts", {}).get("emotion", "neutral"))

    @property
    def tts_style(self) -> str:
        return self.tts_emotion

    @property
    def tts_style_degree(self) -> float | None:
        """Azure Speech style intensity (0.01-2.0). None means use default."""
        val = self.runtime.get("tts", {}).get("styleDegree")
        if val is not None:
            return float(val)
        return None

    @property
    def tts_style_options(self) -> list[str]:
        """Available style names from the top-level 'styles' list."""
        return list(self.expanded.get("styles", []))

    # Legacy aliases
    @property
    def emotion_preset_names(self) -> list[str]:
        return self.tts_style_options

    @property
    def tts_instructions(self) -> str:
        """Legacy — just return the style name."""
        return self.tts_style

    @property
    def tts_voice_rotation(self) -> list[dict]:
        """List of resolved voice rotation entries for multi-session tab assignment.

        Voice rotation entries are preset names from the 'voices' dict.
        Also supports legacy formats:
        - Strings: ["sage", "noa"] → resolved via voice presets
        - Dicts (legacy triple): [{"voice": "sage", "model": "gpt-4o-mini-tts"}]

        Returns normalized list of dicts: [{"voice": "...", "model": "...", "preset": "..."}, ...]
        """
        raw = self.runtime.get("tts", {}).get("voiceRotation", [])
        result = []
        for entry in raw:
            if isinstance(entry, str):
                # Preset name — resolve to full definition
                resolved = self.resolve_voice(entry)
                result.append({
                    "voice": resolved["voice"],
                    "model": resolved["model"],
                    "preset": entry,
                })
            elif isinstance(entry, dict):
                # Legacy dict format — keep for backward compat
                result.append({
                    "voice": entry.get("voice", ""),
                    "model": entry.get("model"),
                    "preset": entry.get("preset", entry.get("voice", "")),
                })
        return result

    @property
    def tts_emotion_rotation(self) -> list[str]:
        """List of styles to cycle through for multi-session tab assignment.
        Reads 'styleRotation' first, falls back to legacy 'emotionRotation'.
        """
        tts = self.runtime.get("tts", {})
        return tts.get("styleRotation", tts.get("emotionRotation", []))

    @property
    def tts_style_rotation(self) -> list[str]:
        """Alias for tts_emotion_rotation."""
        return self.tts_emotion_rotation

    @property
    def tts_random_rotation(self) -> bool:
        """Whether to randomly assign voices/emotions from rotation lists.

        When True (default), new sessions get a random voice/emotion that
        isn't currently in use by another active session. When False, uses
        the legacy sequential assignment (session_idx % len(rotation)).
        """
        return self.runtime.get("tts", {}).get("randomRotation", True)

    @property
    def tts_local_backend(self) -> str:
        """Local TTS backend for scroll readout fallback.

        "termux" — termux-tts-speak via Android TTS (default, best quality)
        "espeak" — espeak-ng (robotic but universal)
        "none"   — no local fallback, always use API TTS
        """
        return self.runtime.get("tts", {}).get("localBackend", "termux")

    @property
    def tts_voice_options(self) -> list[str]:
        """Available voice preset names."""
        return self.voice_preset_names

    @property
    def tts_model_names(self) -> list[str]:
        """Unique TTS model names across all voice presets."""
        models = set()
        for vdef in self.voices.values():
            if isinstance(vdef, dict) and vdef.get("model"):
                models.add(vdef["model"])
        return sorted(models)

    # ─── STT accessors ──────────────────────────────────────────────

    @property
    def stt_model_name(self) -> str:
        return self.runtime.get("stt", {}).get("model", "whisper")

    @property
    def stt_model_def(self) -> dict[str, Any]:
        return self.models.get("stt", {}).get(self.stt_model_name, {})

    @property
    def stt_provider_name(self) -> str:
        return self.stt_model_def.get("provider", "openai")

    @property
    def stt_provider(self) -> dict[str, Any]:
        return self.providers.get(self.stt_provider_name, {})

    @property
    def stt_base_url(self) -> str:
        return self.stt_provider.get("baseUrl", "https://api.openai.com")

    @property
    def stt_api_key(self) -> str:
        return self.stt_provider.get("apiKey", "")

    @property
    def stt_realtime(self) -> bool:
        return bool(self.runtime.get("stt", {}).get("realtime", False))

    @property
    def stt_model_names(self) -> list[str]:
        return list(self.models.get("stt", {}).keys())

    # ─── Realtime accessors ─────────────────────────────────────────

    @property
    def realtime_model_name(self) -> str:
        return self.runtime.get("realtime", {}).get("model", "gpt-realtime")

    @property
    def realtime_model_def(self) -> dict[str, Any]:
        return self.models.get("realtime", {}).get(self.realtime_model_name, {})

    @property
    def realtime_provider_name(self) -> str:
        return self.realtime_model_def.get("provider", "openai")

    @property
    def realtime_provider(self) -> dict[str, Any]:
        return self.providers.get(self.realtime_provider_name, {})

    @property
    def realtime_base_url(self) -> str:
        return self.realtime_provider.get("baseUrl", "https://api.openai.com")

    @property
    def realtime_api_key(self) -> str:
        return self.realtime_provider.get("apiKey", "")

    # ─── Djent integration ─────────────────────────────────────────

    @property
    def djent_enabled(self) -> bool:
        """Whether djent integration is enabled."""
        return bool(
            self.expanded.get("config", {})
            .get("djent", {})
            .get("enabled", False)
        )

    @djent_enabled.setter
    def djent_enabled(self, value: bool) -> None:
        self.raw.setdefault("config", {}).setdefault("djent", {})["enabled"] = value
        self.expanded = _expand_config(self.raw)

    # ─── Extra options ────────────────────────────────────────────

    @property
    def extra_options(self) -> list[dict[str, Any]]:
        """Get extra options from config.

        Each option has: title, description, silent (bool).
        Non-silent options are read aloud in the intro.
        Silent options are only read when scrolled to.
        Includes djent options when djent integration is enabled.
        """
        opts = list(self.expanded.get("extraOptions", []))
        if self.djent_enabled:
            # Add djent options that aren't already present
            existing = {o.get("title", "").lower() for o in opts}
            for djent_opt in _DJENT_EXTRA_OPTIONS:
                if djent_opt["title"].lower() not in existing:
                    opts.append(djent_opt)
        return opts

    @property
    def quick_actions(self) -> list[dict[str, Any]]:
        """Get quick actions from config.

        Each action has: key (single character), label, action (message|command), value.
        Includes djent quick actions when djent integration is enabled.
        """
        actions = list(self.expanded.get("quickActions", []))
        if self.djent_enabled:
            # Add djent actions that don't conflict with existing keys
            existing_keys = {a.get("key", "") for a in actions}
            for djent_action in _DJENT_QUICK_ACTIONS:
                if djent_action["key"] not in existing_keys:
                    actions.append(djent_action)
        return actions

    # ─── Session settings ──────────────────────────────────────────

    @property
    def session_cleanup_timeout(self) -> float:
        """Seconds of inactivity before a non-focused session is auto-removed."""
        return float(
            self.expanded.get("config", {})
            .get("session", {})
            .get("cleanupTimeoutSeconds", 300)
        )

    # ─── Ambient mode settings ────────────────────────────────────

    @property
    def ambient_enabled(self) -> bool:
        """Whether ambient mode is enabled (periodic status updates during silence).

        Disabled by default — enable in config.yml: config.ambient.enabled: true
        """
        return bool(
            self.expanded.get("config", {})
            .get("ambient", {})
            .get("enabled", False)
        )

    @property
    def ambient_initial_delay(self) -> float:
        """Seconds of agent silence before the first ambient update."""
        return float(
            self.expanded.get("config", {})
            .get("ambient", {})
            .get("initialDelaySecs", 30)
        )

    @property
    def ambient_repeat_interval(self) -> float:
        """Seconds between subsequent ambient updates after the first."""
        return float(
            self.expanded.get("config", {})
            .get("ambient", {})
            .get("repeatIntervalSecs", 45)
        )

    # ─── Ring receiver settings ───────────────────────────────────

    @property
    def ring_receiver_enabled(self) -> bool:
        """Whether the UDP ring receiver is enabled (listens for ring-mods events)."""
        return bool(
            self.expanded.get("config", {})
            .get("ringReceiver", {})
            .get("enabled", False)
        )

    @property
    def ring_receiver_port(self) -> int:
        """UDP port for the ring receiver."""
        return int(
            self.expanded.get("config", {})
            .get("ringReceiver", {})
            .get("port", 5555)
        )

    # ─── PulseAudio settings ────────────────────────────────────

    @property
    def pulse_auto_reconnect(self) -> bool:
        """Whether to attempt auto-reconnect when PulseAudio goes down."""
        return bool(
            self.expanded.get("config", {})
            .get("pulseAudio", {})
            .get("autoReconnect", False)
        )

    @property
    def pulse_max_reconnect_attempts(self) -> int:
        """Max consecutive reconnect attempts before giving up."""
        return int(
            self.expanded.get("config", {})
            .get("pulseAudio", {})
            .get("maxReconnectAttempts", 3)
        )

    @property
    def pulse_reconnect_cooldown(self) -> float:
        """Minimum seconds between reconnect attempts."""
        return float(
            self.expanded.get("config", {})
            .get("pulseAudio", {})
            .get("reconnectCooldownSecs", 15)
        )

    # ─── Health monitor settings ─────────────────────────────────

    @property
    def haptic_enabled(self) -> bool:
        """Whether haptic feedback (vibration) is enabled."""
        return bool(
            self.expanded.get("config", {})
            .get("haptic", {})
            .get("enabled", False)
        )

    @property
    def scroll_acceleration(self) -> dict:
        """Scroll acceleration settings.

        Returns dict with keys: enabled, fastThresholdMs, turboThresholdMs,
        fastSkip, turboSkip.
        """
        defaults = {
            "enabled": True,
            "fastThresholdMs": 80,
            "turboThresholdMs": 40,
            "fastSkip": 3,
            "turboSkip": 5,
        }
        sa = self.expanded.get("config", {}).get("scrollAcceleration", {})
        if isinstance(sa, dict):
            defaults.update(sa)
        return defaults

    @property
    def chimes_enabled(self) -> bool:
        """Whether audio chimes/tones are enabled."""
        return bool(
            self.expanded.get("config", {})
            .get("chimes", {})
            .get("enabled", False)
        )

    @property
    def health_monitor_enabled(self) -> bool:
        """Whether agent health monitoring is enabled."""
        return bool(
            self.expanded.get("config", {})
            .get("healthMonitor", {})
            .get("enabled", True)
        )

    @property
    def health_warning_threshold(self) -> float:
        """Seconds since last tool call before a session is flagged as warning."""
        return float(
            self.expanded.get("config", {})
            .get("healthMonitor", {})
            .get("warningThresholdSecs", 300)
        )

    @property
    def health_unresponsive_threshold(self) -> float:
        """Seconds since last tool call before a session is flagged as unresponsive."""
        return float(
            self.expanded.get("config", {})
            .get("healthMonitor", {})
            .get("unresponsiveThresholdSecs", 600)
        )

    @property
    def health_check_interval(self) -> float:
        """Seconds between each health check run."""
        return float(
            self.expanded.get("config", {})
            .get("healthMonitor", {})
            .get("checkIntervalSecs", 30)
        )

    @property
    def health_check_tmux_pane(self) -> bool:
        """Whether to verify the agent's tmux pane is still alive during health checks."""
        return bool(
            self.expanded.get("config", {})
            .get("healthMonitor", {})
            .get("checkTmuxPane", True)
        )

    # ─── Notification settings ───────────────────────────────────

    @property
    def notifications_enabled(self) -> bool:
        """Whether webhook notifications are enabled."""
        return bool(
            self.expanded.get("config", {})
            .get("notifications", {})
            .get("enabled", False)
        )

    @property
    def notifications_cooldown(self) -> float:
        """Minimum seconds between identical notifications."""
        return float(
            self.expanded.get("config", {})
            .get("notifications", {})
            .get("cooldownSecs", 60)
        )

    @property
    def notifications_channels(self) -> list[dict]:
        """Raw notification channel configurations."""
        return list(
            self.expanded.get("config", {})
            .get("notifications", {})
            .get("channels", [])
        )

    # ─── Agent spawner settings ───────────────────────────────────

    @property
    def agent_default_workdir(self) -> str:
        """Default working directory for spawned agents."""
        return str(
            self.expanded.get("config", {})
            .get("agents", {})
            .get("defaultWorkdir", "~")
        )

    @property
    def agent_hosts(self) -> list[dict]:
        """Configured remote hosts for spawning agents.

        Each host has: name, host, workdir (optional).
        """
        return list(
            self.expanded.get("config", {})
            .get("agents", {})
            .get("hosts", [])
        )

    @property
    def key_bindings(self) -> dict[str, str]:
        """User-configurable key bindings. Returns action→key mapping."""
        defaults = DEFAULT_CONFIG.get("config", {}).get("keyBindings", {})
        user = self.expanded.get("config", {}).get("keyBindings", {})
        merged = {**defaults, **user}
        return merged

    @property
    def always_allow_restart_tui(self) -> bool:
        """If True, TUI restart skips confirmation dialog."""
        return bool(
            self.expanded.get("config", {})
            .get("alwaysAllow", {})
            .get("restartTUI", True)
        )

    @property
    def tts_pregenerate_workers(self) -> int:
        """Number of concurrent TTS processes for pregeneration (1-8)."""
        val = (
            self.expanded.get("config", {})
            .get("tts", {})
            .get("pregenerateWorkers", 3)
        )
        return max(1, min(8, int(val)))

    # ─── Dwell settings ──────────────────────────────────────────

    @property
    def dwell_duration(self) -> float:
        """Dwell-to-select duration in seconds.

        Returns 0.0 if dwell is disabled, otherwise the configured duration.
        Handles missing/invalid config gracefully.
        """
        try:
            dwell = self.expanded.get("config", {}).get("dwell", {})
            if not dwell.get("enabled", False):
                return 0.0
            duration = float(dwell.get("durationSeconds", 3.0))
            return max(0.0, duration)
        except (TypeError, ValueError):
            return 0.0

    # ─── Conversation / auto-reply settings ──────────────────────

    @property
    def conversation_auto_reply(self) -> bool:
        """Whether auto-reply is enabled for conversation mode.

        When True and conversation mode is active, single "continue"-style
        choices are auto-selected after a configurable delay.
        """
        try:
            return bool(
                self.expanded.get("config", {})
                .get("conversation", {})
                .get("autoReply", False)
            )
        except (TypeError, ValueError):
            return False

    @property
    def conversation_auto_reply_delay(self) -> float:
        """Delay in seconds before auto-selecting a continuation choice.

        Returns 3.0 by default. Clamped to [0.5, 30.0].
        """
        try:
            val = float(
                self.expanded.get("config", {})
                .get("conversation", {})
                .get("autoReplyDelaySecs", 3.0)
            )
            return max(0.5, min(30.0, val))
        except (TypeError, ValueError):
            return 3.0

    # ─── Scroll settings ──────────────────────────────────────────

    @property
    def scroll_debounce(self) -> float:
        """Minimum seconds between scroll events (default 0.15)."""
        return float(
            self.expanded.get("config", {})
            .get("scroll", {})
            .get("debounce", 0.15)
        )

    @property
    def invert_scroll(self) -> bool:
        """Reverse scroll direction for rings that spin the 'wrong' way."""
        return bool(
            self.expanded.get("config", {})
            .get("scroll", {})
            .get("invert", False)
        )

    # ─── Config mutation ────────────────────────────────────────────

    def set_tts_model(self, model_name: str) -> None:
        """Set the TTS model (legacy — prefer set_tts_voice_preset).

        Finds the first voice preset using this model and switches to it.
        """
        # Find a preset using this model
        for name, vdef in self.raw.get("voices", {}).items():
            if isinstance(vdef, dict) and vdef.get("model") == model_name:
                self.set_tts_voice_preset(name)
                return
        # No preset found — just update raw config
        self.raw.setdefault("config", {}).setdefault("tts", {})["voice"] = model_name
        self.expanded = _expand_config(self.raw)

    def set_tts_voice(self, voice: str) -> None:
        """Set the TTS voice by preset name.

        Accepts either a preset name (e.g. 'sage') or a raw voice string
        for backward compatibility. If it matches a preset name, uses that.
        Otherwise, searches for a preset with matching raw voice string.
        """
        # Direct preset name match
        voices = self.raw.get("voices", {})
        if voice in voices:
            self.set_tts_voice_preset(voice)
            return
        # Search by raw voice string
        for name, vdef in voices.items():
            if isinstance(vdef, dict) and vdef.get("voice") == voice:
                self.set_tts_voice_preset(name)
                return
        # No match — set directly (backward compat)
        self.raw.setdefault("config", {}).setdefault("tts", {})["voice"] = voice
        self.expanded = _expand_config(self.raw)

    def set_tts_voice_preset(self, preset_name: str) -> None:
        """Set the TTS voice by preset name."""
        self.raw.setdefault("config", {}).setdefault("tts", {})["voice"] = preset_name
        self.expanded = _expand_config(self.raw)

    def set_tts_speed(self, speed: float) -> None:
        """Set the TTS speed."""
        self.raw.setdefault("config", {}).setdefault("tts", {})["speed"] = speed
        self.expanded = _expand_config(self.raw)

    def set_tts_emotion(self, emotion: str) -> None:
        """Set the TTS style (legacy name: emotion)."""
        self.raw.setdefault("config", {}).setdefault("tts", {})["style"] = emotion
        self.expanded = _expand_config(self.raw)

    def set_tts_style(self, style: str) -> None:
        """Set the TTS style."""
        self.set_tts_emotion(style)

    def set_stt_model(self, model_name: str) -> None:
        """Set the STT model."""
        self.raw.setdefault("config", {}).setdefault("stt", {})["model"] = model_name
        self.expanded = _expand_config(self.raw)

    def set_stt_realtime(self, enabled: bool) -> None:
        """Toggle STT realtime mode."""
        self.raw.setdefault("config", {}).setdefault("stt", {})["realtime"] = enabled
        self.expanded = _expand_config(self.raw)

    # ─── TTS CLI args ───────────────────────────────────────────────

    def tts_cli_args(self, text: str, voice_override: Optional[str] = None,
                     emotion_override: Optional[str] = None,
                     model_override: Optional[str] = None,
                     speed_override: Optional[float] = None) -> list[str]:
        """Build CLI args for the tts tool based on current config.

        Returns the full argument list (excluding the 'tts' binary itself).
        Optional overrides for voice/emotion/model/speed (used for per-session
        rotation and per-context speed).

        voice_override can be a preset name or a raw voice string.
        model_override overrides the model from the voice preset.
        speed_override overrides the base speed (e.g. per-context speeds).

        Style handling: always passes ``--style <name>`` for both providers.
        The tts CLI handles OpenAI instructions and Azure SSML internally.
        """
        # Resolve voice preset — override or current
        preset_name = voice_override or self.tts_voice_preset
        resolved = self.resolve_voice(preset_name)

        model_name = model_override or resolved["model"]
        provider = resolved.get("provider", "openai")
        base_url = resolved["base_url"]
        api_key = resolved["api_key"]
        voice = resolved["voice"]

        args = [text]

        if provider == "azure-speech":
            args.extend(["--provider", "azure-speech"])
            args.extend(["--base-url", base_url])
            args.extend(["--api-key", api_key])
            args.extend(["--voice", voice])
        else:
            # openai provider
            args.extend(["--base-url", base_url])
            args.extend(["--api-key", api_key])
            args.extend(["--model", model_name])
            args.extend(["--voice", voice])

        args.extend(["--speed", str(speed_override if speed_override is not None else self.tts_speed)])

        # Style: just pass --style for both providers
        style = emotion_override or self.tts_style
        if style:
            args.extend(["--style", style])

        # Azure Speech: add --style-degree for style intensity control
        style_degree = self.tts_style_degree
        if style_degree is not None and "--style" in args:
            args.extend(["--style-degree", str(style_degree)])

        args.extend(["--stdout", "--response-format", "wav"])
        return args

    # ─── STT CLI args ───────────────────────────────────────────────

    def stt_cli_args(self) -> list[str]:
        """Build CLI args for the stt tool based on current config.

        Returns the full argument list (excluding the 'stt' binary itself).
        Always includes --stdin.
        """
        args = ["--stdin"]
        args.extend(["--base-url", self.stt_base_url])
        args.extend(["--api-key", self.stt_api_key])
        args.extend(["--transcription-model", self.stt_model_name])

        if self.stt_realtime:
            supports = self.stt_model_def.get("supportsRealtime", False)
            if supports:
                args.append("--realtime")
                args.extend(["--realtime-model", self.realtime_model_name])

        return args
