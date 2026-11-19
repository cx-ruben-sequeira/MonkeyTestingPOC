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

Works the same on **Windows, macOS and Linux**. You need:

- Python 3.11+
- Node.js 20+
- The Claude Code CLI (`claude`), logged in
- git
- Optional: `gh`, logged in

Run these from the repository folder, in PowerShell, Terminal or any shell:

```bash
pipx install -e .
```

```bash
mt install
```

```bash
mt doctor
```

- `pipx install -e .` puts the `mt` command on your PATH (`mt.exe` on Windows). It must be an editable install (`-e`), because MonkeyTesting runs out of this folder: `web/`, `hooks/`, `runner/` and `config.toml` live here. `uv tool install -e .` works too. With no pipx at all, use `python -m mtlib` in place of `mt`; on macOS and Linux, `bin/mt` also works.
- `mt install` installs the pinned `@playwright/mcp` and `@playwright/test` packages in `runner/`, plus the Chromium build they use. On Linux it also installs Chromium's system libraries (`--with-deps`, may ask for sudo); use `mt install --no-deps` to skip that.
- `mt doctor` prints one line per check: tools and versions, Playwright packages and browser, the `gh` login, the data folder, the server port and Jira. `OK` is fine, `WARN` is optional, and `FIX` says what to do.

OS notes:

- **Windows:** turn on long paths (`mt doctor` prints the PowerShell command when they're off), because run folders can get deep. Hooks are started with the full path of the Python running `mt`, so no shebangs or `.py` file associations are needed.
- **Linux servers:** no display is needed. Playwright runs headless everywhere.
- **Paths with spaces** (such as `C:\Program Files\nodejs` or a home folder named `First Last`) are fine. Programs found on PATH are started by name, and worker files are passed relative to the worker's folder. The one case that fails: `MT_DATA_DIR` points outside the repository, and the path between the two contains a space (for example the repo in `C:\code` and the data in `C:\Users\First Last\mt`). The worker then refuses to start with "Refusing unsafe command-line argument". Put the data folder inside the repository, or in a path without spaces.

Secrets and folders:

- **Secrets** are read from environment variables, or from a git-ignored `.env` file next to `config.toml`, so there's no per-shell `export` / `$env:` difference. For example: `JIRA_API_TOKEN=...`
- **`MT_DATA_DIR`** (in the environment or `.env`) moves `data/`, `runs/` and `logs/` to another folder.

## Run

```bash
mt serve
```

Then open http://127.0.0.1:8766. Under **New session**, enter a URL, pick a mode and a duration, and click **Start**.

**Instructions and constraints** are followed strictly. When a session has instructions, a short interpreter run (no browser) reads them first and turns them into rules. Live monitor shows what it understood under **Your instructions, as understood**:

| You write | It becomes | Enforced by |
|---|---|---|
| a URL or path to test ("make your tests under https://…/riskhub") | focus page, scope *this page and below*, or *this page only* when you say so | the guard: navigation outside is blocked, and a worker whose click lands outside must go back first |
| places to avoid ("don't open /admin") | excluded paths | the guard blocks navigation to them |
| things not to touch ("never press Delete or Export") | blocked actions | the guard blocks clicks and typing on matching elements |
| must-dos ("use project name QA-Auto-1") and anything else | must-do items and notes | every worker's prompt |

Your full text also goes to every worker (Explorer, Tester, Reproducer, Reporter) as a binding section at the top of its system prompt. The Explorer only writes charters inside the focus, and suspects found outside the focus are dropped. Login pages always stay reachable.

**Login comes first.** In a session with a login, each browser worker starts logged out:

1. It opens the app and logs in, with organization pickers such as `/login-organization` and other login pages always allowed.
2. It reports `LOGGED_IN`. The orchestrator also notices on its own when the browser leaves the login pages.
3. From then on, your instructions apply in full, and the worker goes to the focus page.

Before login only the safety guard applies: other sites and destructive actions stay blocked.

The interpreter never guesses a URL from a page name. "Only the Applications page" stays a binding note; write the URL or path if you want it enforced. If the interpreter run fails, a simple parser reads the URLs and "don't …" paths itself.

**Page load wait** (default 120 seconds, 5–600) is how long a page may take to load before it counts as broken. It sets the workers' browser navigation timeout, and the navigation and `expect()` waits in the reproduction tests. Workers are also told to wait that long and reload once before reporting a blank, 404 or "not found" page. Raise it for slow staging hosts; from the CLI use `--page-load 180`. Stop the server with Ctrl+C on any OS: workers are stopped together with their whole process tree (claude → node → chromium).

You can also queue a session from the command line:

```bash
mt start https://staging.myapp.com --mode hybrid --minutes 20 --repeats 5
```

```bash
mt status
```

```bash
mt cancel 3
```

A session runs only while `mt serve` is running. Interrupted tasks go back to the front of the queue when you restart it.

### Deleting sessions

Deleting a session removes all of its data and files:

- every database row of the session (tasks, pages, map edges, suspects, bugs, events, usage)
- its run folder `runs/s<id>/` (videos, screenshots, specs, logs, and the saved credentials, which go first)

Jira tickets the session created are **not** deleted, because they live in Jira. The confirmation lists their keys.

Where to delete:

- **Dashboard:** each row of **Recent sessions** has a 🗑 **Delete** button. Tick several rows to get **Delete selected**. In **Live monitor**, use **Delete session**.
- **Confirmation:** a dialog shows exactly what goes (bugs, suspects, events, disk space, Jira keys) and that it can't be undone. Nothing is deleted until you click **Delete** there.
- **Running sessions:** the button becomes **Cancel & delete**. The session is cancelled, and deleted once its workers have stopped.
- **Incomplete deletes:** if some files can't be removed (for example, locked on Windows), the session stays, shows **delete incomplete**, and you can delete it again to retry.

From the command line (it shows what will go and asks first; `--yes` skips the question):

```bash
mt delete 3 4
```

```bash
mt delete --older-than 30d
```

```bash
mt delete --status finished
```

## Troubleshooting

| Symptom | Fix |
|---|---|
| `mt: command not found` | Run `pipx ensurepath` and open a new terminal, or use `python -m mtlib`. |
| `chromium browser` is `FIX` in `mt doctor` | Run `mt install`. On Linux without sudo, run `mt install --no-deps` and have an admin run `npx playwright install-deps chromium` in `runner/`. |
| Workers fail with "MCP server playwright is failed" | `mt doctor` should show the Playwright packages as `OK`; if it doesn't, run `mt install`. |
| `claude` is `FIX` | Install Claude Code and run `claude` once to log in. If it isn't on PATH, set `[workers] claude_bin` in `config.toml` to its full path. |
| `server port` is `WARN` | Another `mt serve` is running, or something else uses the port. Change `[server] port`. |
| Windows: errors about paths that are too long | Turn on long paths (the command is in `mt doctor`), or set `MT_DATA_DIR` to a short folder such as `C:\mt`. |

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
- **A failure must reach the bug.** A test broken in the same way every time also fails N/N, so failing N times isn't enough. Each spec imports `test`/`expect` from a generated helper, `mt.ts`, which records where every run really ended. A failed run counts only if:
  - it got past the spec's checkpoints: `mt.reached(page, /path/)` (on the bug's page) and `mt.ready(locator)` (the list or dropdown loaded);
  - its `mt.precondition(...)` checks held (for example, "a custom state exists");
  - it didn't end on a login page.

  A spec with no checkpoint can't confirm a bug. Workers are also told that a missing item is only a bug after waiting for the list to load, scrolling it to the end, using its search box and checking that the item exists.
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

Click the **Jira** pill at the top right of the dashboard to open **Jira connection**. Each user enters their own:

- Jira URL, such as `https://yourcompany.atlassian.net`
- Atlassian account email
- API token: create one at id.atlassian.com → Security → API tokens
- Optionally, a project key, needed only to create bugs

**Test connection** checks who the token belongs to.

How the token is handled:

- It's kept **in memory only** while `mt serve` runs. It's never written to disk or the database, never logged, never sent back to the page, and workers never get it.
- Re-enter it after a restart. To keep it across restarts, put `JIRA_API_TOKEN=...` in `.env` instead.
- The URL, email and project key are saved and override `[jira]` in `config.toml`.

**Jira tickets in your instructions.** Name tickets in **Instructions and constraints**, as keys (`AST-1234`) or Jira links, and they're read before the session starts:

- **What's read:** summary, type, status, description, and fields such as *Acceptance Criteria* or *Steps*. Comments are read only when you tick **Also read the tickets' comments**.
- **How it's used:** the ticket text becomes part of the instructions. The interpreter turns it into enforced rules, and every worker gets it.
- **Where it shows:** Live monitor lists the tickets that were read.
- **If it fails:** a ticket that can't be read (no Jira connection, no access, wrong key) shows as *not read*, and workers are told not to guess what it says. Up to 5 tickets per session.

**Open in Jira** needs two clicks, the second one to confirm. It creates the issue through `POST /rest/api/3/issue` with an ADF description (steps, expected, actual, severity, URL, session), then attaches the video and screenshots. Clicking again opens the existing ticket instead of creating a new one.

## Files

```
mtlib/cli.py               CLI: `mt` / `python -m mtlib` (serve, start, status, cancel, install, doctor)
mtlib/setup_tools.py       mt install and mt doctor
mtlib/proc.py              start/stop process trees on Windows, macOS and Linux
pyproject.toml             package + `mt` console script (pipx install -e .)
runner/                    pinned Node packages: Playwright MCP + Playwright Test
.github/workflows/ci.yml   tests on Ubuntu, macOS and Windows
bin/mt                     same CLI for macOS/Linux without installing
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
python -m unittest discover -s tests -v
```

The tests use `tests/fake_claude.py`, which streams scripted stream-json, so they spend no tokens. Jira is tested against a local stub. Two tests run real Playwright against the demo app.

To try it end to end, start the demo app:

```bash
python tests/demo_app/serve.py
```

Then start a session on http://localhost:8099. The demo app has these planted bugs:

- The signup form accepts an empty name.
- The **Generate report** button gets a 500.
- The **Pricing** link leads to a 404.
- The apple search is flaky: it should end up under Not confirmed.
- A **Delete all data** button, which the guard must block.
