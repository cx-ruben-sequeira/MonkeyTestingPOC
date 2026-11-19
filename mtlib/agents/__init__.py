"""Agent roles. Each role is a prompt file plus a marker handler and a finish step, on one interface.

An agent never talks to the dispatcher directly: it reads and writes the store (pages, suspects,
new tasks) and returns the task's result. The dispatcher decides what runs next.
`self.srow` is the session's database row (a dict), `self.task` the task row.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import string
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable
from urllib.parse import urlsplit

from ..config import ROOT, Config
from ..proc import python
from ..store import Store
from ..worker import BROWSER_PREFIX, RunResult, checked_argv, fwd, program, quote_safe, rel_arg, short

PROMPTS = Path(__file__).resolve().parent / "prompts"
GUARD_HOOK = ROOT / "hooks" / "guard.py"
SHOT_HOOK = ROOT / "hooks" / "shot.py"          # PostToolUse: asks for a screenshot after each action
SHOT_PAGE = ROOT / "hooks" / "shot_page.js"     # Playwright MCP --init-page: takes it

# Browser tools that can run arbitrary code or touch the local disk stay off for every role.
BROWSER_DENY = [BROWSER_PREFIX + n for n in ("evaluate", "run_code", "run_code_unsafe", "file_upload", "install", "tabs")]
BASE_DENY = ["WebFetch", "WebSearch", "Task", "Agent", "NotebookEdit", "Bash(rm:*)", "Bash(curl:*)",
             "Bash(wget:*)", "Bash(git:*)", "Bash(ssh:*)"]

MODES = {
    "random": ("Random (monkey) mode. Pick your next action at random from the interactive elements in the "
               "current snapshot (use the seed $seed to vary your choices; do not favour the obvious path). "
               "Mix clicks, odd inputs and navigation. Coverage beats depth."),
    "ai": ("AI-guided mode. Choose each action by expected value: the flows a real user relies on, forms "
           "with validation, state changes, error handling, edges between pages. Go deep on what looks fragile."),
    "hybrid": ("Hybrid mode. Alternate: one AI-guided action chosen by value, then one random action picked "
               "from the current snapshot (seed $seed), and keep going. Follow up anything odd the random step finds."),
}


def load_prompt(name: str) -> str:
    base = os.path.normpath(os.path.abspath(os.fspath(PROMPTS)))
    path = os.path.normpath(os.path.abspath(os.path.join(base, f"{name}.md")))
    if os.path.dirname(path) != base or not os.path.isfile(path):
        raise ValueError(f"Unknown prompt file: {name}")
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def render(name: str, **values) -> str:
    """Prompt file with $placeholders filled; JSON braces in the files are left alone."""
    return string.Template(load_prompt(name)).safe_substitute(**values)


def hook_command(script) -> str:
    """Hook command for a worker's settings JSON: the absolute interpreter plus the script, both with
    forward slashes. Never a bare script name: Windows ignores shebangs."""
    exe, path = Path(python()).as_posix(), fwd(script)
    if os.name == "nt":
        # double quotes work in both cmd.exe and Git Bash (Windows paths can't contain a double quote)
        return f'"{exe}" "{path}"'
    return f"{shlex.quote(exe)} {shlex.quote(path)}"


def command_arg(arg: str, cwd, is_program: bool) -> str:
    """One part of the configured claude command: the program as program() gives it (file name when its
    path has spaces), absolute script paths relative to the working folder, anything else unchanged."""
    if is_program:
        if os.path.isabs(arg):
            full = fwd(arg)
            return full if quote_safe(full) else os.path.basename(arg)
        return program(arg)
    return rel_arg(arg, cwd) if os.path.isabs(arg) else arg


def perm_path(path) -> str:
    """An absolute path in the form Claude Code permission rules match on every OS: POSIX slashes, and a
    Windows drive written as /c/... (C:\\Users\\x -> /c/Users/x). Use it as Read(/<perm_path>/**)."""
    raw = os.fspath(path)
    if re.match(r"^[A-Za-z]:[\\/]", raw):   # a Windows drive path (also testable on POSIX)
        p = raw.replace("\\", "/")
        return "/" + p[0].lower() + p[2:]
    return fwd(raw)


def origin_of(url: str) -> str:
    try:
        p = urlsplit(url or "")
    except ValueError:
        return ""
    return f"{p.scheme}://{p.netloc}".lower() if p.scheme and p.netloc else ""


@dataclass
class Context:
    cfg: Config
    store: Store
    srow: dict                          # the session row
    task: dict
    task_dir: Path                      # runs/s<sid>/t<tid>, holds this run's files
    session_dir: Path                   # runs/s<sid>
    repro_fn: Callable[..., Awaitable[dict]] | None = None
    notes: list[str] = field(default_factory=list)


class Agent:
    kind = ""
    uses_browser = True
    builtin_tools = "Read"

    def __init__(self, ctx: Context):
        self.ctx = ctx
        self.cfg, self.store, self.srow, self.task = ctx.cfg, ctx.store, ctx.srow, ctx.task
        self.sid, self.tid = int(ctx.srow["id"]), int(ctx.task["id"])

    # ------------------------------------------------------------ what the run may do
    @property
    def cwd(self) -> Path:
        return self.ctx.task_dir

    @property
    def browser_dir(self) -> Path:
        return self.ctx.task_dir / "browser"

    @property
    def shots_dir(self) -> Path | None:
        """runs/s<sid>/t<tid>/shots when the session takes a screenshot after every browser action."""
        if self.uses_browser and self.srow.get("shots_every_action"):
            return self.ctx.task_dir / "shots"
        return None

    def allow(self) -> list[str]:
        rules = ["Read(./**)"]
        if self.uses_browser:
            rules.append("mcp__playwright")
        return rules

    def deny(self) -> list[str]:
        return BASE_DENY + BROWSER_DENY + ["Edit", "Write", "Bash"]

    def prompt(self) -> str:
        raise NotImplementedError

    def on_marker(self, name: str, data) -> None:
        """A marker line from the stream (PAGE:, SUSPECT:, ...)."""

    async def finish(self, result: RunResult, stopped: str | None) -> dict:
        """After the run ended (normally, or stopped by a limit). Returns task fields to store."""
        return {}

    def on_failed(self, error: str) -> None:
        """The task failed for good (all attempts used)."""

    # ------------------------------------------------------------ shared helpers
    def common_values(self) -> dict:
        row = self.srow
        seed = row["seed"] or 0
        mode = (row["mode"] or "hybrid").lower()
        auth = "No login is needed."
        if row["has_auth"]:
            where = f" at {row['login_url']}" if row["login_url"] else ""
            auth = (f"The app needs a login{where}. Type the literal text MT_USERNAME into the user/email field "
                    "and MT_PASSWORD into the password field: the browser swaps in the real values, which you "
                    "never see. Never print them.")
        return {
            "url": row["url"], "origin": row["origin"],
            "mode_text": string.Template(MODES.get(mode, MODES["hybrid"])).safe_substitute(seed=seed),
            "seed": seed, "auth": auth,
            "instructions": (row["instructions"] or "").strip() or "(none)",
            "destructive": ("Destructive actions are ALLOWED in this session." if row["allow_destructive"] else
                            "Destructive actions (delete, remove, pay, purchase, transfer, send, invite, "
                            "unsubscribe, reset...) are BLOCKED by a guard; if one is blocked, note it and move on."),
            "budget": self.task["action_budget"] or row["max_actions"],
            "session_id": self.sid, "task_id": self.tid,
        }

    def map_summary(self, limit: int = 60) -> str:
        pages = self.store.pages(self.sid)
        if not pages:
            return "(nothing mapped yet)"
        lines = [f"- {'visited' if p['visited'] else 'not visited'}: {p['url']}" +
                 (f"  ({p['title']})" if p["title"] else "") for p in pages[:limit]]
        if len(pages) > limit:
            lines.append(f"- ... and {len(pages) - limit} more")
        return "\n".join(lines)

    def suspects_summary(self) -> str:
        rows = self.store.suspects(self.sid)
        if not rows:
            return "(none yet)"
        return "\n".join(f"- {r['title']} @ {r['url']} [{r['status']}]" for r in rows[:60])

    def allowed_origins(self) -> list[str]:
        extra = self.srow["allowed_origins"] or []
        return [self.srow["origin"]] + [o for o in extra if o]

    def in_scope(self, url: str) -> bool:
        o = origin_of(url)
        return not o or o in self.allowed_origins()

    def record_page(self, data) -> None:
        if isinstance(data, str):
            data = {"url": data}
        if not isinstance(data, dict):
            return
        url = self.page_url(data.get("url"))
        if not url:
            return
        self.store.upsert_page(self.sid, url, title=_s(data.get("title"), 200), kind=_s(data.get("kind"), 40),
                               visited=bool(data.get("visited", True)),
                               components=[str(c) for c in (data.get("components") or [])][:40])
        for link in data.get("links") or []:
            dst = self.page_url(link)
            if dst:
                self.store.add_edge(self.sid, url, dst)

    def record_edge(self, data) -> None:
        if not isinstance(data, dict):
            return
        src, dst = self.page_url(data.get("from")), self.page_url(data.get("to"))
        if src and dst:
            self.store.add_edge(self.sid, src, dst, _s(data.get("label"), 80))

    def page_url(self, raw) -> str | None:
        """A full in-scope http(s) URL for the map, or None (labels like 'nav' or other sites are dropped)."""
        if not isinstance(raw, str) or not raw.strip():
            return None
        url = self.absolute(raw.strip())
        p = urlsplit(url)
        if p.scheme not in ("http", "https") or not p.netloc or not self.in_scope(url):
            return None
        return url

    def absolute(self, url: str) -> str:
        if url.startswith("/"):
            return self.srow["origin"] + url
        return url

    # ------------------------------------------------------------ run files and command
    def write_files(self) -> tuple[list[str], str]:
        """Settings, MCP config and system-prompt addition for this run; returns (argv, prompt)."""
        d = self.ctx.task_dir
        d.mkdir(parents=True, exist_ok=True)
        self.browser_dir.mkdir(parents=True, exist_ok=True)
        settings = {
            "permissions": {"allow": self.allow(), "deny": self.deny()},
            "hooks": {"PreToolUse": [{"matcher": "mcp__playwright__browser_.*",
                                      "hooks": [{"type": "command", "command": hook_command(GUARD_HOOK)}]}]},
        }
        if self.shots_dir:
            self.shots_dir.mkdir(parents=True, exist_ok=True)
            settings["hooks"]["PostToolUse"] = [{"matcher": "mcp__playwright__browser_.*",
                                                 "hooks": [{"type": "command", "command": hook_command(SHOT_HOOK),
                                                            "timeout": 15}]}]
        files = {"settings.json": json.dumps(settings, indent=1),
                 "append.txt": render("_system", **self.common_values())}
        if self.uses_browser:
            files["mcp.json"] = json.dumps(self.mcp_config(), indent=1)
        for name, content in files.items():
            write_in(d, name, content)
        # paths relative to the run's working folder: a home folder with spaces never reaches the command line
        here = self.cwd
        claude = [command_arg(c, here, i == 0) for i, c in enumerate(self.cfg.claude_cmd)]
        cmd = [*claude, "-p", "--output-format", "stream-json", "--verbose",
               "--permission-mode", "dontAsk", "--settings", rel_arg(d / "settings.json", here),
               "--append-system-prompt-file", rel_arg(d / "append.txt", here), "--strict-mcp-config",
               "--setting-sources", "project",  # no user hooks, plugins or allow rules leak into workers
               "--tools", self.builtin_tools, "--no-session-persistence",
               "--name", f"mt-s{self.sid}-t{self.tid}-{self.kind}"]
        if self.uses_browser:
            cmd += ["--mcp-config", rel_arg(d / "mcp.json", here)]
        if self.cfg.model:
            cmd += ["--model", self.cfg.model]
        if self.cfg.effort:
            cmd += ["--effort", self.cfg.effort]
        if self.cfg.max_budget_usd > 0:
            cmd += ["--max-budget-usd", f"{self.cfg.max_budget_usd:g}"]
        return checked_argv(cmd), self.prompt()

    def mcp_config(self) -> dict:
        viewport = self.srow["viewport"] or self.cfg.viewport
        args = [fwd(self.cfg.mcp_cli), "--isolated", "--output-dir", fwd(self.browser_dir),
                "--allowed-origins", ";".join(self.allowed_origins()),
                "--viewport-size", viewport.replace("x", ","),
                "--timeout-action", str(self.cfg.action_timeout_ms), "--console-level", "warning",
                "--image-responses", "omit"]
        if self.cfg.headless:
            args.append("--headless")
        browser = (self.srow["browser"] or self.cfg.browser or "chromium").lower()
        if browser != "chromium":
            args += ["--browser", browser]
        secrets = self.ctx.session_dir / "secrets.env"
        if secrets.exists():
            args += ["--secrets", fwd(secrets)]
        server = {"command": program("node"), "args": args}
        if self.shots_dir:
            args += ["--init-page", fwd(SHOT_PAGE)]
            server["env"] = {"MT_SHOT_DIR": fwd(self.shots_dir)}
        return {"mcpServers": {"playwright": server}}

    def env(self) -> dict:
        """Paths the hooks read, with forward slashes (Python accepts them on Windows too)."""
        return {"MT_GUARD_FILE": fwd(self.ctx.session_dir / "guard.json"),
                "MT_BLOCK_LOG": fwd(self.ctx.session_dir / "blocked.log"),
                "MT_STOP_FILE": fwd(self.ctx.task_dir / "stop.flag"),
                **({"MT_SHOT_DIR": fwd(self.shots_dir)} if self.shots_dir else {})}


def _s(v, n: int):
    return str(v)[:n] if v not in (None, "") else None


def write_in(directory, name: str, content: str, mode: int = 0o600) -> Path:
    """Writes `name` inside `directory` only (UTF-8), owner read/write unless a mode is given."""
    base = os.path.normpath(os.path.abspath(os.fspath(directory)))
    path = os.path.normpath(os.path.abspath(os.path.join(base, name)))
    if os.path.dirname(path) != base:
        raise ValueError(f"Refusing to write outside {base}: {path}")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    os.chmod(path, mode)  # O_CREAT honours the umask, and an existing file keeps its old mode
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(content)
    return Path(path)


def make(ctx: Context) -> Agent:
    from .explorer import Explorer
    from .reporter import Reporter
    from .reproducer import Reproducer
    from .tester import Tester
    kinds = {"explore": Explorer, "test": Tester, "repro": Reproducer, "report": Reporter}
    return kinds[ctx.task["kind"]](ctx)


__all__ = ["Agent", "Context", "make", "render", "origin_of", "short", "write_in", "hook_command"]
