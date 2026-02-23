"""io-mcp TUI package.

Re-exports the public API from submodules for backwards compatibility.

Modules:
    themes  — Color schemes (Nord, Tokyo Night, Catppuccin, Dracula) and CSS generation
    widgets — ChoiceItem, DwellBar, EXTRA_OPTIONS, _safe_action decorator
    app     — IoMcpApp (main Textual App), TUI controller wrapper
"""

from .themes import COLOR_SCHEMES, DEFAULT_SCHEME, get_scheme, build_css
from .widgets import ChoiceItem, DwellBar, EXTRA_OPTIONS, _safe_action
from .app import IoMcpApp, TUI

# Backwards compatibility alias
_build_css = build_css

__all__ = [
    "COLOR_SCHEMES",
    "DEFAULT_SCHEME",
    "get_scheme",
    "build_css",
    "_build_css",
    "ChoiceItem",
    "DwellBar",
    "EXTRA_OPTIONS",
    "_safe_action",
    "IoMcpApp",
    "TUI",
]
