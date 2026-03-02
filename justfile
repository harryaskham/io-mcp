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
    export IO_MCP_URL=http://localhost:8444/mcp
    export PULSE_SERVER=127.0.0.1
    sleep 5
    claude --agent io-mcp:io-mcp {{ FLAGS }} "Register with io-mcp"

tmux-clio *FLAGS:
    tmux split-window -v -t io-mcp-tui:1.1 "just loop clio-local {{ FLAGS }}"

tmux-attach:
    tmux new -A -s io-mcp-tui || true

tmux *FLAGS:
    just tmux-new {{ FLAGS }}
    just tmux-clio
    just tmux-attach

# Dev on desktop, routing audio to phone via PulseAudio over Tailscale
desktop *FLAGS:
    IO_MCP_URL=http://localhost:8444/mcp \
    PULSE_SERVER=samsung-sm-s928b.miku-owl.ts.net \
    uv run io-mcp --dev {{ FLAGS }}

phone-local *FLAGS:
    IO_MCP_URL=http://localhost:8444/mcp \
    PULSE_SERVER=127.0.0.1 \
    uv run io-mcp --dev {{ FLAGS }}

phone *FLAGS:
    IO_MCP_URL=http://samsung-sm-s928b.miku-owl.ts.net:8444/mcp \
    PULSE_SERVER=samsung-sm-s928b.miku-owl.ts.net \
    uv run io-mcp --dev {{ FLAGS }}

default *FLAGS:
    just {{ FLAGS }} --default-config

# Run all tests
test *FLAGS:
    uv run python -m pytest tests/ {{ FLAGS }}

# Run tests with short output
test-q *FLAGS:
    uv run python -m pytest tests/ -q --tb=short {{ FLAGS }}

# Run tests matching a keyword (e.g. just test-k chat)
test-k PATTERN *FLAGS:
    uv run python -m pytest tests/ -q --tb=short -k "{{ PATTERN }}" {{ FLAGS }}

# Run a specific test file (e.g. just test-f tests/test_tts.py)
test-f FILE *FLAGS:
    uv run python -m pytest {{ FILE }} --tb=short {{ FLAGS }}

# Run tests with verbose failure output
test-v *FLAGS:
    uv run python -m pytest tests/ -v --tb=long {{ FLAGS }}

demo:
    just run --demo --port 8499
