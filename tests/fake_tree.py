#!/usr/bin/env python3
"""Stand-in for `claude -p` that starts a long-running child (like Playwright MCP -> chromium), writes the
child's PID next to itself, prints one stream-json line, then waits. Used to prove a stop kills the tree."""
import json
import os
import subprocess
import sys
import time

pid_file = os.path.join(os.getcwd(), "child.pid")
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
with open(pid_file + ".tmp", "w", encoding="utf-8") as fh:
    fh.write(str(child.pid))
os.replace(pid_file + ".tmp", pid_file)
print(json.dumps({"type": "system", "subtype": "init", "session_id": "tree", "mcp_servers": []}), flush=True)
time.sleep(120)
