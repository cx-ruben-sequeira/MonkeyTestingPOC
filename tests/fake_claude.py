#!/usr/bin/env python3
"""Stand-in for `claude -p` in tests: reads the prompt on stdin, streams a scripted run per role.

FAKE_CLAUDE_MODE: normal (default) | crash_once | crash | loop | slow | budget
FAKE_CLAUDE_STATE: folder for crash_once bookkeeping
"""
import json
import os
import re
import sys
import tempfile
import time

prompt = sys.stdin.buffer.read().decode("utf-8", errors="replace")
role = (re.search(r"ROLE:\s*(\w+)", prompt) or re.search(r"(x)", "x")).group(1)
mode = os.environ.get("FAKE_CLAUDE_MODE", "normal")
delay = float(os.environ.get("FAKE_CLAUDE_DELAY", "0.02"))
system = ""
if "--append-system-prompt-file" in sys.argv:
    with open(sys.argv[sys.argv.index("--append-system-prompt-file") + 1], encoding="utf-8") as fh:
        system = fh.read()
BASE = (re.search(r"Target application: (\S+)", system) or re.search(r"(http://demo\.test)", "http://demo.test")).group(1)
BASE = BASE.rstrip("/")
n = 0


def emit(obj):
    print(json.dumps(obj), flush=True)
    time.sleep(delay)


def say(text):
    global n
    n += 1
    emit({"type": "assistant", "message": {"id": f"m{n}", "content": [{"type": "text", "text": text}],
                                           "usage": {"output_tokens": 40}}})


def tool(name, args, result="ok"):
    global n
    n += 1
    tid = f"t{n}"
    emit({"type": "assistant", "message": {"id": f"m{n}", "content": [
        {"type": "tool_use", "id": tid, "name": name, "input": args}], "usage": {"output_tokens": 20}}})
    emit({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": tid,
                                                   "content": [{"type": "text", "text": result}]}]}})


def browser(verb, args, url=None):
    tool(f"mcp__playwright__browser_{verb}", args, f"### Page state\n- Page URL: {url or BASE + '/'}\n")


def done(text, cost=0.05):
    emit({"type": "result", "subtype": "success", "is_error": False, "total_cost_usd": cost, "result": text,
          "usage": {"input_tokens": 100, "output_tokens": 300}})


emit({"type": "system", "subtype": "init", "session_id": f"fake-{role}",
      "mcp_servers": [{"name": "playwright", "status": "connected"}]})

if mode == "crash" or (mode == "crash_once" and role == "explorer"):
    flag = os.path.join(os.environ.get("FAKE_CLAUDE_STATE") or tempfile.gettempdir(), "crashed-once")
    if mode == "crash" or not os.path.exists(flag):
        open(flag, "w").close()
        print("boom: simulated crash", file=sys.stderr)
        sys.exit(3)

def login():
    """Like a real worker in a session with a login: landing page, the tenant picker, then the app."""
    if "LOGIN FIRST" not in system:
        return
    browser("navigate", {"url": BASE + "/"}, BASE + "/")
    browser("type", {"element": "Organization", "ref": "o1", "text": "acme"}, BASE + "/login-organization")
    browser("type", {"element": "Username", "ref": "u1", "text": "MT_USERNAME"}, BASE + "/auth/realms/acme/login-actions/authenticate")
    browser("click", {"element": "Sign in button", "ref": "s1"}, BASE + "/applicationRiskManagement")
    if mode != "login_auto":
        say('LOGGED_IN: {"url": "' + BASE + '/applicationRiskManagement"}')


if role == "explorer":
    login()
    browser("navigate", {"url": BASE + "/"})
    say("\n".join([
        f'PAGE: {json.dumps({"url": BASE + "/", "title": "Demo Shop", "kind": "list", "components": ["list: products"], "links": [BASE + "/signup", BASE + "/reports", BASE + "/pricing"]})}',
        f'EDGE: {json.dumps({"from": BASE + "/", "to": BASE + "/signup", "label": "Sign up"})}',
    ]))
    browser("click", {"element": "Sign up link", "ref": "e3"}, BASE + "/signup")
    say(f'PAGE: {json.dumps({"url": BASE + "/signup", "title": "Sign up", "kind": "form", "components": ["form: name, email"]})}')
    browser("click", {"element": "Reports link", "ref": "e4"}, BASE + "/reports")
    say("\n".join([
        f'PAGE: {json.dumps({"url": BASE + "/reports", "title": "Reports", "kind": "other"})}',
        f'CHARTER: {json.dumps({"area": "Signup form", "goal": "validation", "start_url": BASE + "/signup", "focus": ["empty", "long"]})}',
        f'CHARTER: {json.dumps({"area": "Reports", "goal": "server errors", "start_url": BASE + "/reports", "focus": ["errors"]})}',
    ]))
    done("Demo shop with signup and reports.")
elif role == "tester":
    area = (re.search(r"Charter: (.+)", prompt) or re.search(r"(?s)(.)", "?")).group(1).strip()
    login()
    if mode == "loop":
        for _ in range(20):
            browser("click", {"element": "Same button", "ref": "e9"}, BASE + "/signup")
        done("looped")
        sys.exit(0)
    if mode == "budget":
        for i in range(50):
            browser("click", {"element": f"Button {i}", "ref": f"e{i}"}, BASE + "/signup")
        done("spent")
        sys.exit(0)
    if mode == "slow":
        for i in range(200):
            browser("click", {"element": f"Slow button {i}", "ref": f"e{i}"}, BASE + "/signup")
            time.sleep(0.2)
    # a blocked destructive click, as the guard hook would report it
    tool("mcp__playwright__browser_click", {"element": "Delete all data button", "ref": "e7"},
         "MonkeyTesting guard blocked this action: 'Delete all data button' looks destructive")
    browser("type", {"element": "Name", "ref": "e1", "text": ""}, BASE + "/signup")
    browser("click", {"element": "Create account button", "ref": "e2"}, BASE + "/signup")
    if "Signup" in area:
        sus = {"title": "Signup accepts an empty name", "url": BASE + "/signup",
               "steps": [f"Open {BASE}/signup", "Leave Name empty", "Click Create account"],
               "expected": "Error 'Name is required'", "actual": "Shows 'Welcome, !'", "severity": "medium",
               "confidence": 0.9, "evidence": []}
        flaky = {"title": "Search for apple sometimes shows no results", "url": BASE + "/search",
                 "steps": [f"Open {BASE}/search?q=apple"], "expected": "Apple listed", "actual": "No results",
                 "severity": "low", "confidence": 0.5}
        say(f"SUSPECT: {json.dumps(sus)}\nSUSPECT: {json.dumps(flaky)}")
        say(f"SUSPECT: {json.dumps(sus)}")  # duplicate on purpose: must be dropped
    else:
        sus = {"title": "Generate report returns 500", "url": BASE + "/reports",
               "steps": [f"Open {BASE}/reports", "Click Generate report"], "expected": "Report ready",
               "actual": "Something went wrong; GET /api/report -> 500", "severity": "high", "confidence": 0.95,
               "evidence": ["network: GET /api/report -> 500"]}
        say(f"SUSPECT: {json.dumps(sus)}")
    done(f"Tested {area}.")
elif role == "reproducer":
    spec = os.path.join(os.getcwd(), "repro.spec.ts")
    with open(spec, "w", encoding="utf-8") as fh:
        fh.write("import { test, expect } from '@playwright/test';\ntest('repro', async ({ page }) => {});\n")
    tool("Write", {"file_path": spec})
    say('SPEC: {"file": "repro.spec.ts", "assertion": "expect(...)", "status": "fails"}')
    done("spec written")
elif role == "interpreter":
    if mode == "rules_fail":
        done("I could not work out any rules.")
        sys.exit(0)
    block = prompt.split("<<<", 1)[-1].split(">>>", 1)[0]
    urls = re.findall(r"https?://\S+", block)
    rules = {"focus_urls": [u.rstrip(".,") for u in urls if "/admin" not in u],
             "scope": "page" if "only this page" in block else "below",
             "exclude_paths": ["/admin/**"] if "admin" in block else [],
             "forbidden_actions": ["export"] if "export" in block.lower() else [],
             "required": ["use the name Ana"] if "Ana" in block else [], "notes": ["focus on breaking things"],
             "summary": "test only the given area"}
    say("RULES: " + json.dumps(rules))
    done("rules")
elif role == "reporter":
    say('DUPLICATE: {"bug": 999, "of": 1}')
    done("## Summary\nThe demo shop was explored and two defects were confirmed.\n\n## Recommendations\n- Fix them.")
else:
    done("unknown role")
