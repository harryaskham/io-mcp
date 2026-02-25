# io-mcp development recipes

# Base: run io-mcp --dev with any extra flags (e.g. just dev --local --default-config)
dev *FLAGS:
    uv run io-mcp --dev {{ FLAGS }}

# Dev mode with default config (ignore user config)
default *FLAGS:
    just dev --default-config {{ FLAGS }}

# Dev on desktop, routing audio to phone via PulseAudio over Tailscale
dev-desktop *FLAGS:
    IO_MCP_URL=http://localhost:8444/mcp \
    PULSE_SERVER=samsung-sm-s928b.miku-owl.ts.net \
    uv run io-mcp --dev {{ FLAGS }}

# Desktop with default config
default-desktop *FLAGS:
    just dev-desktop --default-config {{ FLAGS }}

tmux *FLAGS:
    tmux new -A -s io-mcp-tui "while true; do just rmconfig; just dev-desktop {{ FLAGS }}; sleep 3; done"

rmconfig:
    rm $HOME/.config/io-mcp/config.yml || true
