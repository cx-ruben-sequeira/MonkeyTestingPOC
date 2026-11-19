#!/usr/bin/env python3
"""PostToolUse hook for Playwright MCP browser tools: one screenshot after every browser action.

Active only when MT_SHOT_DIR is set (the session ticked "Screenshot every action"). For each action
it writes "<time_ns>-<verb>.req" into that folder, waits until hooks/shot_page.js (running inside the
Playwright MCP server) has saved "<same name>.png", then writes "<same name>.json" with what the action
was. Waiting keeps the next action from starting before the picture of this one is taken. The hook
never blocks or changes the tool result: on any problem it just gives up quietly.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time

# Read-only tools: they don't change the page, so a screenshot would only repeat the previous one.
SKIP = {"browser_snapshot", "browser_take_screenshot", "browser_console_messages", "browser_network_requests",
        "browser_close", "browser_tabs", "browser_install"}
WAIT_S = 8.0


def shot_dir() -> str | None:
    raw = os.environ.get("MT_SHOT_DIR") or ""
    if not raw:
        return None
    d = os.path.normpath(os.path.abspath(raw))
    return d if os.path.basename(d) == "shots" and os.path.isdir(d) else None


def describe(args: dict) -> str:
    for k in ("element", "url", "key", "text", "startElement"):
        v = args.get(k)
        if v:
            return str(v)[:200]
    if args.get("fields"):
        return ", ".join(str(f.get("name") or "") for f in args["fields"] if isinstance(f, dict))[:200]
    return ""


def main() -> int:
    d = shot_dir()
    if not d:
        return 0
    try:
        payload = json.load(sys.stdin)
    except ValueError:
        return 0
    tool = str(payload.get("tool_name") or "")
    verb = tool.rsplit("__", 1)[-1]
    if not verb.startswith("browser_") or verb in SKIP:
        return 0
    args = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
    name = f"{time.time_ns()}-{re.sub(r'[^a-z_]', '', verb[len('browser_'):])[:30]}"
    base = os.path.join(d, name)
    with open(base + ".req", "w", encoding="utf-8") as fh:
        fh.write(verb)
    end = time.time() + WAIT_S
    while time.time() < end:
        if os.path.exists(base + ".png") or os.path.exists(base + ".err"):
            break
        time.sleep(0.05)
    else:
        try:
            os.unlink(base + ".req")  # nobody answered (no browser yet): don't leave a stale request
        except OSError:
            pass
        return 0
    if os.path.exists(base + ".png"):
        with open(base + ".json", "w", encoding="utf-8") as fh:
            json.dump({"time": time.time(), "action": verb[len("browser_"):], "target": describe(args)}, fh)
    return 0


if __name__ == "__main__":
    sys.exit(main())
