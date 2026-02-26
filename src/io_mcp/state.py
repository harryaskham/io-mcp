"""Persistent UI state for io-mcp.

Stores toggle states and preferences that survive restarts.
Written to ~/.config/io-mcp/state.json (small, fast, no merging needed).
"""

from __future__ import annotations

import json
import os
from typing import Any

from .config import DEFAULT_CONFIG_DIR

STATE_FILE = os.path.join(DEFAULT_CONFIG_DIR, "state.json")


def _load() -> dict[str, Any]:
    """Load state from disk. Returns empty dict if missing/corrupt."""
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, PermissionError):
        return {}


def _save(state: dict[str, Any]) -> None:
    """Save state to disk. Best effort — never raises."""
    try:
        os.makedirs(DEFAULT_CONFIG_DIR, exist_ok=True)
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)
    except Exception:
        pass


def get(key: str, default: Any = None) -> Any:
    """Get a state value."""
    return _load().get(key, default)


def set(key: str, value: Any) -> None:
    """Set a state value and persist."""
    state = _load()
    state[key] = value
    _save(state)


def toggle(key: str, default: bool = False) -> bool:
    """Toggle a boolean state value. Returns the new value."""
    state = _load()
    current = state.get(key, default)
    new_value = not current
    state[key] = new_value
    _save(state)
    return new_value
