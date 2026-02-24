"""Configuration system for io-mcp.

Reads/writes config from $HOME/.config/io-mcp/config.yml (or --config-file).
Also merges with a local .io-mcp.yml if found in the current directory
(local takes precedence over global config).

Config strings can include shell variables like ${OPENAI_API_KEY} which are
expanded at load time.

The config defines:
  - providers: named API endpoints with baseUrl and apiKey
  - models: stt, tts, and realtime model definitions with provider associations
  - config: runtime settings (selected models, voice, speed, etc.)
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
        "tts": {
            "gpt-4o-mini-tts": {
                "provider": "openai",
                "voice": {
                    "default": "sage",
                    "options": [
                        "alloy", "ash", "ballad", "coral", "echo",
                        "fable", "onyx", "nova", "sage", "shimmer", "verse",
                    ],
                },
            },
            "mai-voice-1": {
                "provider": "azure-speech",
                "voice": {
                    "default": "en-US-Noa:MAI-Voice-1",
                    "options": [
                        "en-US-Noa:MAI-Voice-1",
                        "en-US-Teo:MAI-Voice-1",
                    ],
                },
            },
        },
    },
    "config": {
        "colorScheme": "nord",
        "realtime": {
            "model": "gpt-realtime",
        },
        "tts": {
            "model": "mai-voice-1",
            "voice": "en-US-Noa:MAI-Voice-1",
            "uiVoice": "en-US-Teo:MAI-Voice-1",
            "speed": 1.3,
            "emotion": "shy",
            "localBackend": "termux",  # "termux", "espeak", or "none"
            "voiceRotation": [],
            "emotionRotation": [],
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
            "dashboard": "d",
            "unifiedInbox": "a",
            "agentLog": "g",
            "hotReload": "r",
            "quit": "q",
        },
        "djent": {
            "enabled": False,
        },
    },
    "emotionPresets": {
        "happy": "Speak in a warm, cheerful, and upbeat tone. Sound genuinely pleased and positive.",
        "calm": "Speak in a soothing, relaxed, and measured tone. Be gentle and unhurried.",
        "excited": "Speak with high energy and enthusiasm. Sound genuinely thrilled and animated.",
        "serious": "Speak in a focused, professional, and matter-of-fact tone. Be clear and direct.",
        "friendly": "Speak in a warm, conversational, and approachable tone. Like talking to a good friend.",
        "neutral": "Speak in a natural, even tone without strong emotion.",
        "storyteller": "Speak like a captivating narrator. Vary pace and emphasis for dramatic effect.",
        "gentle": "Speak softly and kindly, as if comforting someone. Warm and tender.",
        "shy": "Speak in a soft, quiet whisper. Be hesitant and gentle, as if sharing a secret. Keep volume low and intimate.",
    },
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
    def load(cls, config_path: Optional[str] = None) -> "IoMcpConfig":
        """Load config from file, creating with defaults if not found.

        Also merges with a local .io-mcp.yml if found in the current directory.
        Local config takes precedence over the global config.
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

        # Merge local .io-mcp.yml (cwd takes precedence)
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

        # Check required top-level keys
        for key in ("providers", "models", "config"):
            if key not in self.raw:
                warnings.append(f"Missing top-level key '{key}' — using defaults")

        # Check TTS model exists in models
        tts_model = self.runtime.get("tts", {}).get("model", "")
        tts_models = self.raw.get("models", {}).get("tts", {})
        if tts_model and tts_model not in tts_models:
            warnings.append(f"TTS model '{tts_model}' not found in models.tts — available: {list(tts_models.keys())}")

        # Check STT model exists in models
        stt_model = self.runtime.get("stt", {}).get("model", "")
        stt_models = self.raw.get("models", {}).get("stt", {})
        if stt_model and stt_model not in stt_models:
            warnings.append(f"STT model '{stt_model}' not found in models.stt — available: {list(stt_models.keys())}")

        # Check TTS voice is valid for current model
        tts_voice = self.runtime.get("tts", {}).get("voice", "")
        if tts_model and tts_model in tts_models:
            model_def = tts_models[tts_model]
            voice_options = model_def.get("voice", {}).get("options", [])
            if voice_options and tts_voice and tts_voice not in voice_options:
                warnings.append(f"TTS voice '{tts_voice}' not in options for {tts_model}: {voice_options}")

        # Check providers referenced by models exist
        for category in ("tts", "stt"):
            for name, model_def in self.raw.get("models", {}).get(category, {}).items():
                provider = model_def.get("provider", "")
                if provider and provider not in self.raw.get("providers", {}):
                    warnings.append(f"Model '{name}' references provider '{provider}' which is not defined")

        # Check emotion preset exists
        emotion = self.runtime.get("tts", {}).get("emotion", "")
        presets = self.raw.get("emotionPresets", {})
        if emotion and presets and emotion not in presets:
            # Not an error — could be a custom instruction string
            pass

        # Check speed is in valid range
        speed = self.runtime.get("tts", {}).get("speed", 1.0)
        if not isinstance(speed, (int, float)) or speed < 0.1 or speed > 5.0:
            warnings.append(f"TTS speed {speed} is out of range (0.1-5.0)")

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
                                    "agent_disconnected", "error"}
                    for evt in events:
                        if evt not in valid_events:
                            warnings.append(
                                f"Notification channel '{ch_name}' has unknown event '{evt}' — "
                                f"expected one of: {', '.join(sorted(valid_events))}"
                            )

            cooldown = notif_cfg.get("cooldownSecs", 60)
            if isinstance(cooldown, (int, float)) and cooldown < 0:
                warnings.append(f"notifications.cooldownSecs ({cooldown}) cannot be negative")

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

    # ─── TTS accessors ──────────────────────────────────────────────

    @property
    def tts_model_name(self) -> str:
        return self.runtime.get("tts", {}).get("model", "gpt-4o-mini-tts")

    @property
    def tts_model_def(self) -> dict[str, Any]:
        return self.models.get("tts", {}).get(self.tts_model_name, {})

    @property
    def tts_provider_name(self) -> str:
        return self.tts_model_def.get("provider", "openai")

    @property
    def tts_provider(self) -> dict[str, Any]:
        return self.providers.get(self.tts_provider_name, {})

    @property
    def tts_base_url(self) -> str:
        return self.tts_provider.get("baseUrl", "https://api.openai.com")

    @property
    def tts_api_key(self) -> str:
        return self.tts_provider.get("apiKey", "")

    @property
    def tts_voice(self) -> str:
        return self.runtime.get("tts", {}).get("voice", "sage")

    @property
    def tts_ui_voice(self) -> str:
        """Get the UI voice. Falls back to regular voice if not set."""
        ui = self.runtime.get("tts", {}).get("uiVoice", "")
        return ui if ui else self.tts_voice

    @property
    def tts_speed(self) -> float:
        return float(self.runtime.get("tts", {}).get("speed", 1.0))

    @property
    def tts_emotion(self) -> str:
        return self.runtime.get("tts", {}).get("emotion", "neutral")

    @property
    def tts_instructions(self) -> str:
        """Get the TTS instructions text for the current emotion preset."""
        emotion = self.tts_emotion
        presets = self.expanded.get("emotionPresets", {})
        # If the emotion matches a preset, use its text
        if emotion in presets:
            return presets[emotion]
        # Otherwise treat the emotion value itself as custom instructions
        return emotion

    @property
    def emotion_preset_names(self) -> list[str]:
        return list(self.expanded.get("emotionPresets", {}).keys())

    @property
    def tts_voice_rotation(self) -> list[str]:
        """List of voices to cycle through for multi-session tab assignment."""
        return self.runtime.get("tts", {}).get("voiceRotation", [])

    @property
    def tts_emotion_rotation(self) -> list[str]:
        """List of emotions to cycle through for multi-session tab assignment."""
        return self.runtime.get("tts", {}).get("emotionRotation", [])

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
        voice_def = self.tts_model_def.get("voice", {})
        return voice_def.get("options", [])

    @property
    def tts_model_names(self) -> list[str]:
        return list(self.models.get("tts", {}).keys())

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
            .get("autoReconnect", True)
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
            .get("reconnectCooldownSecs", 30)
        )

    # ─── Health monitor settings ─────────────────────────────────

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

    # ─── Config mutation ────────────────────────────────────────────

    def set_tts_model(self, model_name: str) -> None:
        """Set the TTS model and reset voice to the new model's default."""
        self.raw.setdefault("config", {}).setdefault("tts", {})["model"] = model_name
        # Reset voice to the new model's default
        model_def = self.raw.get("models", {}).get("tts", {}).get(model_name, {})
        voice_def = model_def.get("voice", {})
        default_voice = voice_def.get("default", "")
        if default_voice:
            self.raw["config"]["tts"]["voice"] = default_voice
        self.expanded = _expand_config(self.raw)

    def set_tts_voice(self, voice: str) -> None:
        """Set the TTS voice."""
        self.raw.setdefault("config", {}).setdefault("tts", {})["voice"] = voice
        self.expanded = _expand_config(self.raw)

    def set_tts_speed(self, speed: float) -> None:
        """Set the TTS speed."""
        self.raw.setdefault("config", {}).setdefault("tts", {})["speed"] = speed
        self.expanded = _expand_config(self.raw)

    def set_tts_emotion(self, emotion: str) -> None:
        """Set the TTS emotion preset (or custom instructions)."""
        self.raw.setdefault("config", {}).setdefault("tts", {})["emotion"] = emotion
        self.expanded = _expand_config(self.raw)

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
                     emotion_override: Optional[str] = None) -> list[str]:
        """Build CLI args for the tts tool based on current config.

        Returns the full argument list (excluding the 'tts' binary itself).
        Optional overrides for voice/emotion (used for per-session rotation).
        """
        provider = self.tts_provider_name
        voice = voice_override or self.tts_voice
        args = [text]

        if provider == "azure-speech":
            args.extend(["--provider", "azure-speech"])
            args.extend(["--base-url", self.tts_base_url])
            args.extend(["--api-key", self.tts_api_key])
            args.extend(["--voice", voice])
        else:
            # openai provider
            args.extend(["--base-url", self.tts_base_url])
            args.extend(["--api-key", self.tts_api_key])
            args.extend(["--model", self.tts_model_name])
            args.extend(["--voice", voice])

        args.extend(["--speed", str(self.tts_speed)])

        # Add emotion/instructions (override or default)
        emotion = emotion_override or self.tts_emotion
        presets = self.expanded.get("emotionPresets", {})
        if provider == "azure-speech":
            # Azure Speech uses SSML <mstts:express-as style="STYLE">
            # Pass the preset name (e.g. "happy") not the text description.
            # If emotion is a preset name, use it directly as the SSML style.
            # If it's custom text (not a preset), pass it as-is (user may
            # be providing a valid SSML style name).
            style = emotion if emotion else ""
            if style:
                args.extend(["--instructions", style])
        else:
            # OpenAI: resolve preset name to full text instructions
            instructions = presets.get(emotion, emotion) if emotion else self.tts_instructions
            if instructions:
                args.extend(["--instructions", instructions])

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
