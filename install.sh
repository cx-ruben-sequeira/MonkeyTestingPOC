#!/usr/bin/env bash
# One-time setup: pinned Playwright MCP + Playwright Test (package.json) and the Chromium build they use.
set -euo pipefail
cd "$(dirname "$0")"
command -v python3 >/dev/null || { echo "python3 (3.11+) is required"; exit 1; }
command -v node >/dev/null || { echo "Node.js 18+ is required"; exit 1; }
command -v claude >/dev/null || echo "warning: the claude CLI isn't on PATH; set [workers] claude_bin in config.toml"
npm ci --no-audit --no-fund
npx playwright install chromium
chmod +x bin/mt hooks/guard.py tests/demo_app/serve.py
mkdir -p data runs logs
echo "Done. Start the dashboard with: bin/mt serve   (then open http://127.0.0.1:8766)"
