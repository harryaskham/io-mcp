# io-mcp development recipes

rmconfig *FLAGS:
    rm $HOME/.config/io-mcp/config.yml || true
    just {{ FLAGS }}


# Base: run io-mcp with any extra flags (e.g. just run --local --default-config)
run *FLAGS:
    uv run io-mcp {{ FLAGS }}

loop *FLAGS:
    while true; do just {{ FLAGS }}; sleep 3; done

tmux-new *FLAGS:
    tmux new-session -d -s io-mcp-tui "just {{ FLAGS }}"

clio-local *FLAGS:
    clio-local {{ FLAGS }}

tmux-clio *FLAGS:
    tmux split-window -h -l '50%' -t io-mcp-tui:1.1 "just {{ FLAGS }} clio-local"

tmux-attach:
    tmux new -A -s io-mcp-tui

tmux *FLAGS:
    just tmux-new {{ FLAGS }}
    just tmux-clio {{ FLAGS }}
    just tmux-attach

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
