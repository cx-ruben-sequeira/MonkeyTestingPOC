#!/usr/bin/env python3
"""PreToolUse hook for Playwright MCP browser tools (QA/staging safety guard).

Blocks clicks/typing/form fills on elements whose description looks destructive (delete, pay,
transfer, ...) unless the session allows destructive actions, any navigation off the session's
allowed origins, and everything the user's instructions rule out (as understood by the interpreter):
navigation outside the focus area or onto excluded paths, and clicks/typing on forbidden elements.
When a click lands outside the focus area the orchestrator writes MT_SCOPE_FILE; until the worker is
back, every action except going back or navigating to an in-scope page is refused.
Session rules come from the JSON file named in MT_GUARD_FILE:
  {"origins": [...], "allow_destructive": false, "forbidden": ["export"],
   "scope": {"app_url": ..., "mode": "app|below|page", "focus": [...], "exclude": [...], "login_url": ...}}
Blocked actions are appended to MT_BLOCK_LOG and reported back to the agent with a tag the
orchestrator recognises ("MonkeyTesting guard").
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from urllib.parse import urlsplit

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from mtlib.scope import Scope  # noqa: E402  (stdlib-only module)

DESTRUCTIVE = re.compile(
    r"\b(delete|remove|destroy|drop|erase|purge|wipe|truncate|reset all|clear all|pay|payment|purchase|buy|"
    r"checkout|check out|place order|order now|transfer|withdraw|send|invite|unsubscribe|deactivate|"
    r"close account|cancel subscription|terminate|revoke|archive all|apagar|eliminar|remover|excluir|pagar)\b",
    re.I)
READ_ONLY = ("browser_snapshot", "browser_take_screenshot", "browser_console_messages", "browser_network_requests",
             "browser_wait_for", "browser_navigate_back", "browser_close")
INTERACT = ("browser_click", "browser_type", "browser_fill_form", "browser_select_option", "browser_press_key",
            "browser_drag", "browser_check", "browser_uncheck")


def _load(path_env: str) -> dict:
    raw = os.environ.get(path_env) or ""
    if not raw:
        return {}
    path = os.path.normpath(os.path.abspath(raw))
    if not path.endswith(".json") or not os.path.isfile(path):
        return {}
    with open(path, "r", encoding="utf-8") as fh:
        try:
            return json.load(fh)
        except ValueError:
            return {}


def _log(text: str) -> None:
    raw = os.environ.get("MT_BLOCK_LOG") or ""
    if not raw:
        return
    path = os.path.normpath(os.path.abspath(raw))
    if not path.endswith(".log") or not os.path.isdir(os.path.dirname(path)):
        return
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {text}\n")


def _scope_message() -> str:
    """Set by the orchestrator when the browser landed outside the focus area."""
    raw = os.environ.get("MT_SCOPE_FILE") or ""
    if not raw:
        return ""
    path = os.path.normpath(os.path.abspath(raw))
    if not path.endswith(".flag") or not os.path.isfile(path):
        return ""
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read(600).strip()


def logged_in() -> bool:
    """MT_LOGIN_FILE exists once the worker is logged in. Before that, the user's focus rules don't apply:
    the worker must be able to reach the app's landing and login pages. Origins and safety still apply."""
    raw = os.environ.get("MT_LOGIN_FILE") or ""
    if not raw:
        return True
    path = os.path.normpath(os.path.abspath(raw))
    return path.endswith(".flag") and os.path.isfile(path)


def _stop_message() -> str:
    """Set by the orchestrator when this worker hit a limit: every browser action is refused from then on."""
    raw = os.environ.get("MT_STOP_FILE") or ""
    if not raw:
        return ""
    path = os.path.normpath(os.path.abspath(raw))
    if not path.endswith(".flag") or not os.path.isfile(path):
        return ""
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read(500).strip() or "this worker's action budget is used up."


def origin(url: str) -> str:
    try:
        p = urlsplit(url)
    except ValueError:
        return ""
    return f"{p.scheme}://{p.netloc}".lower() if p.scheme and p.netloc else ""


def texts_of(tool: str, args: dict) -> list[str]:
    out = [str(args.get(k) or "") for k in ("element", "startElement", "endElement", "key")]
    if tool.endswith("browser_fill_form"):
        out += [str(f.get("name") or "") for f in args.get("fields") or [] if isinstance(f, dict)]
    return [t for t in out if t]


def decide(tool: str, args: dict, rules: dict) -> str | None:
    """Reason to block, or None to allow."""
    short = tool.rsplit("__", 1)[-1]
    origins = [o.lower().rstrip("/") for o in rules.get("origins") or []]
    if short in ("browser_navigate", "browser_tabs") and args.get("url"):
        o = origin(str(args["url"]))
        if origins and o not in origins:
            return f"navigation to {o or args['url']} is outside the allowed origins ({', '.join(origins)})"
        if rules.get("scope") and logged_in():
            why = Scope.from_rules(rules).reason(str(args["url"]))
            if why:
                return f"navigation to {args['url']} breaks the user's instructions: {why}"
    if short in INTERACT:
        for t in texts_of(tool, args):
            low = t.lower()
            for word in rules.get("forbidden") or []:
                if word and str(word).lower() in low:
                    return f"'{t[:80]}' matches '{word}', which the user's instructions say not to use"
    if short in INTERACT and not rules.get("allow_destructive"):
        for t in texts_of(tool, args):
            m = DESTRUCTIVE.search(t)
            if m:
                return f"'{t[:80]}' looks destructive ('{m.group(0)}') and destructive actions are off for this session"
    return None


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except ValueError:
        return 0
    tool = str(payload.get("tool_name") or "")
    args = payload.get("tool_input") or {}
    if "browser_" not in tool or not isinstance(args, dict):
        return 0
    stop = _stop_message()
    if stop:
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse", "permissionDecision": "deny",
            "permissionDecisionReason": f"MonkeyTesting limit: {stop} Do not use the browser again. Print every "
                                        "marker line you still owe (PAGE, CHARTER, SUSPECT, SPEC...) now and finish."}}))
        return 0
    rules = _load("MT_GUARD_FILE")
    off = _scope_message() if logged_in() else ""
    name = tool.rsplit("__", 1)[-1]
    if off and name not in READ_ONLY and not (name == "browser_navigate" and args.get("url")):
        start = Scope.from_rules(rules).start_url if rules.get("scope") else ""
        reason = (f"you are outside the area the user's instructions allow ({off}). Go back with "
                  f"browser_navigate_back" + (f" or browser_navigate to {start}" if start else "")
                  + " before doing anything else")
    else:
        reason = decide(tool, args, rules)
    if not reason:
        return 0
    _log(f"BLOCKED {tool}: {reason}")
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse", "permissionDecision": "deny",
        "permissionDecisionReason": f"MonkeyTesting guard blocked this action: {reason}. Pick another action."}}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
