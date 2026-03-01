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
        scrollbar-background: {s['bg_alt']};
        scrollbar-background-hover: {s['bg_alt']};
        scrollbar-background-active: {s['bg_alt']};
        scrollbar-color: {s['border']};
        scrollbar-color-hover: {s['fg_dim']};
        scrollbar-color-active: {s['accent']};
        scrollbar-corner-color: {s['bg_alt']};
    }}

    /* ─── Tab bar (replaces Header) ─────────────────────────── */

    #tab-bar {{
        dock: top;
        height: auto;
        width: 1fr;
        background: {s['bg_alt']};
        layout: horizontal;
    }}

    #tab-bar-left {{
        width: 1fr;
        height: auto;
        min-height: 1;
        color: {s['accent']};
        padding: 0 1;
    }}

    #tab-bar-right {{
        width: auto;
        height: auto;
        min-height: 1;
        color: {s['fg_dim']};
        padding: 0 1;
        content-align: right middle;
    }}

    /* ─── Daemon status line (legacy, hidden — merged into tab bar) ── */

    #daemon-status {{
        display: none;
    }}

    /* ─── Agent prompt / preamble ──────────────────────────── */

    #preamble {{
        margin: 0 1;
        padding: 1 2;
        color: {s['success']};
        background: {s['bg_alt']};
        border-left: thick {s['success']};
        width: 1fr;
        height: auto;
        text-style: bold;
    }}

    /* ─── Status / waiting message ─────────────────────────── */

    #status {{
        margin: 0 1;
        padding: 1 2;
        color: {s['warning']};
        width: 1fr;
        height: auto;
    }}

    /* ─── Agent activity indicator ─────────────────────────── */

    #agent-activity {{
        margin: 0 1;
        padding: 0 2;
        height: auto;
        color: {s['blue']};
        width: 1fr;
        display: none;
    }}

    /* ─── Speech log ──────────────────────────────────────── */

    #speech-log {{
        margin: 0 1;
        height: auto;
        max-height: 5;
        padding: 0 2;
        background: {s['bg']};
    }}

    .speech-entry {{
        color: {s['fg_dim']};
        margin: 0;
        padding: 0;
        width: 1fr;
    }}

    /* ─── Two-column inbox layout ─────────────────────────── */

    #main-content {{
        layout: horizontal;
        height: 1fr;
        width: 1fr;
        background: {s['bg']};
    }}

    #inbox-list {{
        width: 30;
        min-width: 20;
        max-width: 40;
        height: 1fr;
        border-right: tall {s['border']};
        background: {s['bg']};
        padding: 0;
        overflow-x: hidden;
        display: none;
        scrollbar-background: {s['bg']};
        scrollbar-background-hover: {s['bg']};
        scrollbar-background-active: {s['bg']};
        scrollbar-color: {s['border']};
        scrollbar-color-hover: {s['fg_dim']};
        scrollbar-color-active: {s['accent']};
    }}

    #choices-panel {{
        width: 1fr;
        height: 1fr;
    }}

    /* ─── Inbox list item styling ─────────────────────────── */

    InboxListItem {{
        padding: 0 1;
        height: auto;
        width: 1fr;
        margin: 0;
        background: {s['bg']};
    }}

    InboxListItem > .inbox-label {{
        color: {s['fg']};
        width: 1fr;
        height: auto;
    }}

    InboxListItem.-highlight {{
        background: {s['highlight_bg']};
    }}

    InboxListItem.-highlight > .inbox-label {{
        color: {s['highlight_fg']};
        text-style: bold;
    }}

    /* ─── Choices list ────────────────────────────────────── */

    #choices {{
        margin: 0 1;
        height: 1fr;
        overflow-x: hidden;
        padding: 1 0;
        background: {s['bg_alt']};
        scrollbar-background: {s['bg_alt']};
        scrollbar-background-hover: {s['bg_alt']};
        scrollbar-background-active: {s['bg_alt']};
        scrollbar-color: {s['border']};
        scrollbar-color-hover: {s['fg_dim']};
        scrollbar-color-active: {s['accent']};
    }}

    /* ─── Tmux pane view ──────────────────────────────────── */

    #pane-view {{
        margin: 0 1;
        height: 1fr;
        border: tall {s['border']};
        background: {s['bg_alt']};
        color: {s['fg']};
        display: none;
        scrollbar-background: {s['bg_alt']};
        scrollbar-background-hover: {s['bg_alt']};
        scrollbar-background-active: {s['bg_alt']};
        scrollbar-color: {s['border']};
        scrollbar-color-hover: {s['fg_dim']};
        scrollbar-color-active: {s['accent']};
    }}

    /* ─── Choice item styling ─────────────────────────────── */

    ChoiceItem {{
        padding: 0 2;
        height: auto;
        width: 1fr;
        margin: 0 0;
        background: {s['bg_alt']};
        border-bottom: dashed {s['border']};
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
        border-left: thick {s['accent']};
    }}

    ChoiceItem.-highlight > .choice-label {{
        background: {s['highlight_bg']};
        color: {s['highlight_fg']};
        text-style: bold;
    }}

    ChoiceItem.-highlight > .choice-summary {{
        background: {s['highlight_bg']};
        color: {s['highlight_accent']};
    }}

    /* ─── Dwell countdown bar ─────────────────────────────── */

    #dwell-bar {{
        margin: 0 2;
        color: {s['warning']};
        height: 1;
    }}

    /* ─── Bottom status line ─────────────────────────────────── */

    #footer-status {{
        dock: bottom;
        height: 1;
        background: {s['bg_alt']};
        color: {s['fg_dim']};
        padding: 0 1;
        margin-top: 1;
    }}

    /* ─── Input fields ────────────────────────────────────── */

    #filter-input {{
        margin: 0 1;
        display: none;
        border: tall {s['purple']};
        background: {s['bg_alt']};
    }}

    /* ─── Text Input Modal ─────────────────────────────────── */

    TextInputModal {{
        align: center middle;
    }}

    TextInputModal > Vertical {{
        width: 80%;
        max-width: 100;
        height: auto;
        max-height: 20;
        border: tall {s['accent']};
        background: {s['bg_alt']};
        padding: 1 2;
    }}

    TextInputModal > Vertical > #modal-title {{
        width: 1fr;
        height: auto;
        margin-bottom: 1;
        color: {s['accent']};
        text-style: bold;
    }}

    TextInputModal > Vertical > #modal-hint {{
        width: 1fr;
        height: auto;
        margin-bottom: 1;
        color: {s['fg_dim']};
    }}

    TextInputModal > Vertical > #modal-text-input {{
        height: auto;
        min-height: 3;
        max-height: 10;
        border: tall {s['border']};
        background: {s['bg']};
    }}

    TextInputModal > Vertical > #modal-text-input .text-area--cursor-line {{
        background: {s['bg']};
    }}

    TextInputModal > Vertical > #modal-text-input .text-area--selection {{
        background: {s['border']};
    }}

    /* ─── Hide Textual Header (replaced by tab-bar) ───────── */

    Header {{
        display: none;
    }}

    /* ─── Global scrollbar theming for all scrollable widgets ── */

    ListView {{
        scrollbar-background: {s['bg_alt']};
        scrollbar-background-hover: {s['bg_alt']};
        scrollbar-background-active: {s['bg_alt']};
        scrollbar-color: {s['border']};
        scrollbar-color-hover: {s['fg_dim']};
        scrollbar-color-active: {s['accent']};
    }}

    /* ─── Chat bubble view ─────────────────────────────────── */

    #chat-feed {{
        display: none;
        width: 1fr;
        height: 1fr;
        scrollbar-size: 1 1;
        scrollbar-background: {s['bg']};
        scrollbar-background-hover: {s['bg']};
        scrollbar-color: {s['border']};
        scrollbar-color-hover: {s['fg_dim']};
        scrollbar-color-active: {s['accent']};
        background: {s['bg']};
    }}

    #chat-choices {{
        display: none;
        dock: bottom;
        width: 1fr;
        height: auto;
        max-height: 40%;
        background: {s['bg_alt']};
        border: tall {s['accent']};
        margin: 0 1;
        scrollbar-background: {s['bg_alt']};
        scrollbar-color: {s['border']};
        scrollbar-color-hover: {s['fg_dim']};
    }}

    #chat-choices:focus {{
        border: tall {s['highlight_accent']};
    }}

    /* ─── Preamble header in chat-choices list ─────────────── */

    PreambleItem {{
        padding: 1 2;
        height: auto;
        width: 1fr;
        background: {s['bg_alt']};
        border-left: thick {s['success']};
        border-bottom: dashed {s['border']};
    }}

    PreambleItem > .preamble-text {{
        color: {s['success']};
        width: 1fr;
        height: auto;
        text-style: bold;
    }}

    PreambleItem.-highlight {{
        background: {s['bg_alt']};
    }}

    PreambleItem.-highlight > .preamble-text {{
        color: {s['success']};
    }}

    #chat-input-bar {{
        display: none;
        dock: bottom;
        height: auto;
        min-height: 3;
        max-height: 8;
        layout: horizontal;
        width: 1fr;
        margin: 0 1 1 1;
    }}

    #chat-input {{
        width: 1fr;
        height: auto;
        min-height: 3;
        max-height: 8;
        border: tall {s['border']};
        background: {s['bg_alt']};
    }}

    #chat-voice-btn {{
        width: 6;
        height: auto;
        min-height: 3;
        content-align: center middle;
        border: tall {s['border']};
        background: {s['bg_alt']};
        color: {s['accent']};
    }}

    #chat-input .text-area--cursor-line {{
        background: {s['bg_alt']};
    }}

    #chat-input .text-area--cursor {{
        color: {s['bg_alt']};
        background: {s['accent']};
    }}

    #chat-input .text-area--placeholder {{
        color: {s['fg_dim']};
    }}

    #chat-input:focus {{
        border: tall {s['accent']};
    }}

    ChatBubbleItem {{
        padding: 0 2;
        height: auto;
        width: 1fr;
        background: {s['bg']};
        border: solid {s['border']};
        margin: 0 1 0 1;
    }}

    /* ─── Bubble kind variants (left-border color coding) ─── */

    ChatBubbleItem.-speech {{
        border-left: thick {s['accent']};
    }}

    ChatBubbleItem.-choices {{
        border-left: thick {s['warning']};
    }}

    ChatBubbleItem.-user-msg {{
        border-left: thick {s['purple']};
        margin-left: 6;
    }}

    ChatBubbleItem.-system {{
        border: none;
        background: transparent;
        padding: 0 2;
    }}

    /* ─── Bubble inner elements ──────────────────────────── */

    ChatBubbleItem > .chat-bubble-text {{
        width: 1fr;
        height: auto;
        color: {s['fg']};
    }}

    ChatBubbleItem > .chat-bubble-ts {{
        width: 1fr;
        height: auto;
        color: {s['fg_dim']};
    }}

    ChatBubbleItem > .chat-bubble-choice {{
        width: 1fr;
        height: auto;
        color: {s['fg']};
    }}

    ChatBubbleItem > .chat-bubble-choice-selected {{
        width: 1fr;
        height: auto;
        color: {s['success']};
    }}

    ChatBubbleItem > .chat-bubble-choice-dim {{
        width: 1fr;
        height: auto;
        color: {s['fg_dim']};
    }}

    ChatBubbleItem > .chat-bubble-pending {{
        width: 1fr;
        height: auto;
        color: {s['warning']};
    }}

    ChatBubbleItem > .chat-bubble-system {{
        width: 1fr;
        height: auto;
        color: {s['fg_dim']};
    }}

    ChatBubbleItem > .chat-bubble-header {{
        width: 1fr;
        height: auto;
        color: {s['fg_dim']};
        text-style: dim;
    }}

    ChatBubbleItem.-header {{
        border: none;
        background: transparent;
        padding: 0 2;
        margin-top: 1;
    }}

    /* ─── Highlighted (focused) bubble ───────────────────── */

    ChatBubbleItem.-highlight {{
        background: {s['highlight_bg']};
        border: solid {s['accent']};
    }}

    ChatBubbleItem.-highlight.-speech {{
        border-left: thick {s['accent']};
    }}

    ChatBubbleItem.-highlight.-choices {{
        border-left: thick {s['warning']};
    }}

    ChatBubbleItem.-highlight.-user-msg {{
        border-left: thick {s['purple']};
    }}

    ChatBubbleItem.-highlight.-system {{
        border: none;
        background: {s['highlight_bg']};
    }}

    TextArea {{
        scrollbar-background: {s['bg']};
        scrollbar-background-hover: {s['bg']};
        scrollbar-background-active: {s['bg']};
        scrollbar-color: {s['border']};
        scrollbar-color-hover: {s['fg_dim']};
        scrollbar-color-active: {s['accent']};
    }}
    """
