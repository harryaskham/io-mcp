"""Color schemes and CSS generation for io-mcp TUI.

Supports Nord (default), Tokyo Night, Catppuccin, and Dracula themes.
Each scheme defines colors for backgrounds, text, accents, and highlights.
"""

from __future__ import annotations


# ─── Color Schemes ────────────────────────────────────────────────────────

COLOR_SCHEMES: dict[str, dict[str, str]] = {
    "nord": {
        "bg": "#2e3440",
        "bg_alt": "#3b4252",
        "fg": "#eceff4",
        "fg_dim": "#616e88",
        "accent": "#88c0d0",
        "success": "#a3be8c",
        "warning": "#ebcb8b",
        "error": "#bf616a",
        "purple": "#b48ead",
        "blue": "#81a1c1",
        "highlight_bg": "#434c5e",
        "highlight_fg": "#eceff4",
        "highlight_accent": "#88c0d0",
        "border": "#4c566a",
    },
    "tokyo-night": {
        "bg": "#1a1b26",
        "bg_alt": "#24283b",
        "fg": "#a9b1d6",
        "fg_dim": "#565f89",
        "accent": "#7aa2f7",
        "success": "#9ece6a",
        "warning": "#e0af68",
        "error": "#f7768e",
        "purple": "#bb9af7",
        "blue": "#7dcfff",
        "highlight_bg": "#292e42",
        "highlight_fg": "#c0caf5",
        "highlight_accent": "#7aa2f7",
        "border": "#414868",
    },
    "catppuccin": {
        "bg": "#1e1e2e",
        "bg_alt": "#313244",
        "fg": "#cdd6f4",
        "fg_dim": "#585b70",
        "accent": "#89b4fa",
        "success": "#a6e3a1",
        "warning": "#f9e2af",
        "error": "#f38ba8",
        "purple": "#cba6f7",
        "blue": "#74c7ec",
        "highlight_bg": "#45475a",
        "highlight_fg": "#cdd6f4",
        "highlight_accent": "#89b4fa",
        "border": "#585b70",
    },
    "dracula": {
        "bg": "#282a36",
        "bg_alt": "#44475a",
        "fg": "#f8f8f2",
        "fg_dim": "#6272a4",
        "accent": "#8be9fd",
        "success": "#50fa7b",
        "warning": "#f1fa8c",
        "error": "#ff5555",
        "purple": "#bd93f9",
        "blue": "#8be9fd",
        "highlight_bg": "#44475a",
        "highlight_fg": "#f8f8f2",
        "highlight_accent": "#bd93f9",
        "border": "#6272a4",
    },
}

DEFAULT_SCHEME = "nord"


def get_scheme(name: str = DEFAULT_SCHEME) -> dict[str, str]:
    """Get a color scheme by name, with fallback to default."""
    return COLOR_SCHEMES.get(name, COLOR_SCHEMES[DEFAULT_SCHEME])


def build_css(scheme_name: str = DEFAULT_SCHEME) -> str:
    """Build the Textual CSS using a named color scheme."""
    s = COLOR_SCHEMES.get(scheme_name, COLOR_SCHEMES[DEFAULT_SCHEME])
    return f"""
    Screen {{
        background: {s['bg']};
    }}

    #tab-bar {{
        margin: 0 1;
        height: 1;
        color: {s['accent']};
        background: {s['bg_alt']};
        padding: 0 1;
        border-bottom: solid {s['border']};
    }}

    #daemon-status {{
        margin: 0 2 0 2;
        height: 1;
        color: {s['fg_dim']};
        width: 1fr;
        padding: 0 1;
    }}

    #preamble {{
        margin: 1 2 0 2;
        padding: 1 1;
        color: {s['success']};
        width: 1fr;
        height: auto;
        text-style: bold;
        border-bottom: solid {s['border']};
    }}

    #status {{
        margin: 0 2 0 2;
        padding: 1 1;
        color: {s['warning']};
        width: 1fr;
        height: auto;
    }}

    #agent-activity {{
        margin: 0 2;
        padding: 0 1;
        height: auto;
        color: {s['blue']};
        width: 1fr;
        display: none;
    }}

    #speech-log {{
        margin: 0 2;
        height: auto;
        max-height: 5;
        padding: 0 1;
        border-top: solid {s['border']};
        border-bottom: solid {s['border']};
    }}

    .speech-entry {{
        color: {s['fg_dim']};
        margin: 0;
        padding: 0;
        width: 1fr;
    }}

    #choices {{
        margin: 0 1;
        height: 1fr;
        overflow-x: hidden;
        padding: 1 0;
    }}

    #pane-view {{
        margin: 0 1;
        height: 1fr;
        border: solid {s['border']};
        background: {s['bg_alt']};
        color: {s['fg']};
        display: none;
    }}

    ChoiceItem {{
        padding: 0 2;
        height: auto;
        width: 1fr;
        margin: 0 0;
    }}

    ChoiceItem > .choice-label {{
        color: {s['fg']};
        width: 1fr;
        height: auto;
    }}

    ChoiceItem > .choice-summary {{
        color: {s['fg_dim']};
        margin-left: 4;
        width: 1fr;
        height: auto;
    }}

    ChoiceItem.-highlight {{
        background: {s['highlight_bg']};
    }}

    ChoiceItem.-highlight > .choice-label {{
        color: {s['highlight_fg']};
        text-style: bold;
    }}

    ChoiceItem.-highlight > .choice-summary {{
        color: {s['highlight_accent']};
    }}

    #dwell-bar {{
        margin: 0 2;
        color: {s['warning']};
        height: 1;
    }}

    #footer-help {{
        dock: bottom;
        height: 1;
        background: {s['bg_alt']};
        color: {s['fg_dim']};
        padding: 0 1;
    }}

    #freeform-input {{
        margin: 1 2;
        display: none;
        border: tall {s['accent']};
    }}

    #filter-input {{
        margin: 0 2;
        display: none;
        border: tall {s['purple']};
    }}

    Header {{
        background: {s['bg']};
        color: {s['accent']};
    }}
    """
