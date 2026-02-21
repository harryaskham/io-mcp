#!/usr/bin/env bash
# Force-rebuild io-mcp: clear stale bytecache, reinstall, and run.
set -euo pipefail
cd "$(git -C "$(dirname "$0")" rev-parse --show-toplevel)"
find src -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
uv sync --reinstall-package io-mcp
exec uv run io-mcp "$@"
