# io-mcp development recipes

rmconfig:
    rm $HOME/.config/io-mcp/config.yml || true

# Base: run io-mcp with any extra flags (e.g. just run --local --default-config)
run *FLAGS:
    uv run io-mcp {{ FLAGS }}

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
dev-desktop-rmconfig *FLAGS:
    just rmconfig
    just dev-desktop {{ FLAGS }}

dev-desktop-rmconfig-tmux *FLAGS:
    tmux new -A -s io-mcp-tui "while true; do just dev-desktop-rmconfig {{ FLAGS }}; sleep 3; done"

# Desktop with default config
dev-desktop-default *FLAGS:
    just dev-desktop --default-config {{ FLAGS }}
