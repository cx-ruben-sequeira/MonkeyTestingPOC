# MonkeyTesting

A local platform for autonomous exploratory QA. You give it the URL of a QA or staging app. A pool of headless Claude workers explores the app through Playwright MCP and reports suspected bugs. Each suspect is turned into a Playwright test and run **N times** (N comes from the New session form, default 5). A suspect counts as a **Bug** only if it fails all N runs on the same assertion. Each bug comes with a video, steps to reproduce, expected and actual results, and an **Open in Jira** button.

```
Dashboard (web/index.html) ──HTTP/JSON──► mtlib/server.py ──► SQLite (data/mt.db)
                                                               ▲
Orchestrator (mtlib/dispatcher.py): one pool of N worker slots shared by all sessions
  per session:  Explorer ─► Tester × charters ─► Reproducer per suspect ─► Reporter
  each worker = headless `claude -p` + its own Playwright MCP (isolated browser, own output dir)
```

## Setup

Requires Python 3.11+, Node 18+ and the `claude` CLI, logged in.

```bash
./install.sh
```

This installs the pinned `@playwright/mcp` and `@playwright/test` packages and the Chromium build they use.

## Run

```bash
bin/mt serve
```

Then open http://127.0.0.1:8766. Under **New session**, enter a URL, pick a mode and a duration, and click **Start**.

You can also queue a session from the command line:

```bash
bin/mt start https://staging.myapp.com --mode hybrid --minutes 20 --repeats 5
```

```bash
bin/mt status
```

```bash
bin/mt cancel 3
```

A session runs only while `bin/mt serve` is running. Interrupted tasks go back to the front of the queue when you restart it.

## Roles

| Worker | Does | Reports (marker lines in the stream) |
|---|---|---|
| Explorer | Maps menus, routes and forms, then splits the app into test charters | `PAGE:` `EDGE:` `CHARTER:` |
| Tester | Works one charter. Tries valid, invalid, boundary and unexpected inputs, checks console and network, compares expected with actual | `PAGE:` `ACTION:` `SUSPECT:` |
| Reproducer | Turns a suspect into `repro.spec.ts`, with the expected result as the assertion | `SPEC:` |
| Reporter | Marks duplicates and writes the summary, coverage and recommendations | `DUPLICATE:` + Markdown |

Prompts are in `mtlib/agents/prompts/`. Each role is one class in `mtlib/agents/` with the same interface: prompt, marker handler, finish step.

**Modes:** Random picks seeded random actions from the current snapshot. AI-guided picks actions by value. Hybrid alternates between the two.

## Rules the orchestrator enforces (not the agents)

- **Priority:** Reproducer > Explorer > Tester > Reporter. A new suspect queues its Reproducer right away.
- **Limits:** a session has a deadline and a maximum number of browser actions, and every `browser_*` call counts. At the limit, testers are stopped and queued testers are dropped. Reproducers still finish, then the Reporter runs.
- **N-of-N:** `mtlib/repro.py` runs the spec N times (one Playwright run each, so progress shows live) and reads the JSON reports itself. A suspect is a Bug only if all N runs failed on the same `expect()`. Otherwise it is **Not confirmed (k/N)**.
- **Shared context:** each tester sees the map, the charters already done and the suspects already found. Duplicate suspects (same page and same title words) are dropped.
- **Loop guard:** if the same action on the same page is repeated more than `loop_repeat_limit` times, that worker is stopped.
- **Recovery:** a crashed worker is retried (`max_attempts`). If the Explorer fails for good, the session falls back to one whole-app charter.

## Safety (QA and staging only)

- Playwright MCP runs `--isolated --headless --allowed-origins <target>` with its own output dir for each worker.
- Each worker gets its own settings file with `--permission-mode dontAsk`, `--strict-mcp-config` and `--setting-sources project`:
  - Explorer and Tester: Playwright MCP tools and `Read` only.
  - Reproducer: also Write/Edit inside its suspect folder and `Bash(npx playwright test …)`.
  - `browser_evaluate`, `browser_run_code` and `browser_file_upload` are denied, as are WebFetch, WebSearch and rm.
- `hooks/guard.py` (PreToolUse) blocks clicks and typing on elements that look destructive (delete, pay, transfer, send, invite, unsubscribe…) and any navigation outside the allowed origins. To lift this for a session, tick **Allow destructive actions**. Blocked actions show in the event log with ⛔.
- If the URL host has no staging, qa, test, dev, sandbox or localhost label, the form shows a warning and needs an extra tick before it starts.
- **Credentials** go to `runs/s<id>/secrets.env` (mode 0600). Playwright MCP substitutes them through `--secrets`, so the model only ever types `MT_USERNAME` and `MT_PASSWORD`. They never appear on a command line or in a prompt, they are masked in logs, and they are not stored in the database.
- The dashboard listens only on 127.0.0.1. POSTs need the `X-MT` header and a same-origin `Origin`, and the Host header is checked.

## Jira

Fill in `[jira]` in `config.toml` (`base_url`, `email`, `project_key`, `issue_type`) and export the token:

```bash
export JIRA_API_TOKEN=...
```

The token is read only from the environment and is never stored. **Open in Jira** needs two clicks, the second one to confirm. It creates the issue through `POST /rest/api/3/issue` with an ADF description (steps, expected, actual, severity, URL, session), then attaches the video and screenshots. Clicking again opens the existing ticket instead of creating a new one.

## Files

```
bin/mt                     CLI
config.toml                server, workers, playwright, limits, jira
mtlib/store.py             SQLite (fixed prepared statements only)
mtlib/dispatcher.py        pool, session lifecycle, limits, loop guard, retries
mtlib/worker.py            claude -p runner: argv check, stdin prompt, stream-json parser, stop/kill
mtlib/agents/*.py          explorer, tester, reproducer, reporter
mtlib/agents/prompts/*.md  role prompts (+ _system.md appended to every worker)
mtlib/repro.py             N runs of Playwright Test, N-of-N rule, video and screenshot pick
mtlib/jira.py              Jira REST v3
mtlib/server.py            dashboard API and media (Range support for video)
hooks/guard.py             PreToolUse safety guard
web/index.html             dashboard: Dashboard, New session, Live monitor, Map, Bugs, Report
tests/                     unit tests (fake Claude, Jira stub) + demo app with planted bugs
```

## Tests

```bash
python3 -m unittest discover -s tests -v
```

The tests use `tests/fake_claude.py`, which streams scripted stream-json, so they spend no tokens. Jira is tested against a local stub. Two tests run real Playwright against the demo app.

To try it end to end, start the demo app:

```bash
python3 tests/demo_app/serve.py
```

Then start a session on http://localhost:8099. The demo app has these planted bugs:

- The signup form accepts an empty name.
- The **Generate report** button gets a 500.
- The **Pricing** link leads to a 404.
- The apple search is flaky: it should end up under Not confirmed.
- A **Delete all data** button, which the guard must block.
