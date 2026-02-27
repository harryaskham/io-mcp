# io-mcp development recipes

rmconfig *FLAGS:
    rm $HOME/.config/io-mcp/config.yml || true
    just {{ FLAGS }}


# Base: run io-mcp with any extra flags (e.g. just run --local --default-config)
run *FLAGS:
    uv run io-mcp {{ FLAGS }}

loop *FLAGS:
    while true; do just {{ FLAGS }}; sleep 3; done

tmux *FLAGS:
    tmux new -A -s io-mcp-tui "just {{ FLAGS }}"

# Base: run io-mcp --dev with any extra flags (e.g. just dev --local --default-config)
dev *FLAGS:
    uv run io-mcp --dev {{ FLAGS }}

# Dev on desktop, routing audio to phone via PulseAudio over Tailscale
desktop *FLAGS:
    IO_MCP_URL=http://localhost:8444/mcp \
    PULSE_SERVER=samsung-sm-s928b.miku-owl.ts.net \
    uv run io-mcp --dev {{ FLAGS }}

phone *FLAGS:
    IO_MCP_URL=http://samsung-sm-s928b.miku-owl.ts.net:8444/mcp \
    PULSE_SERVER=samsung-sm-s928b.miku-owl.ts.net \
    uv run io-mcp --dev {{ FLAGS }}

default *FLAGS:
    just {{ FLAGS }} --default-config

demo:
    just run --demo --port 8499
