"""Generic headless `claude -p` runner used by every agent role.

Free text never goes on the command line: the prompt goes on stdin, and the tool rules, MCP config
and extra instructions go into per-task files. Every argument must survive shlex.quote unchanged
(paths use forward slashes, so this holds on Windows too), and it never goes through a shell.
The stream-json output is parsed live: marker lines (PAGE:, SUSPECT:, ...) become structured
events, every Playwright browser_* tool call counts as one action, and the run can be stopped at
any time (the whole process tree is stopped through mtlib/proc.py, on any OS).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shlex
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import proc as procs

log = logging.getLogger("mt.worker")

MARKERS = ("PAGE", "EDGE", "CHARTER", "SUSPECT", "ACTION", "SPEC", "DUPLICATE", "NOTE", "RULES", "LOGGED_IN")
MARKER_RE = re.compile(r"^\s*[`*]*\s*(" + "|".join(MARKERS) + r"):\s*(.*?)\s*[`*]*\s*$")
CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
PAGE_URL_RE = re.compile(r"Page URL:\s*(\S+)")
FLAG_RE = re.compile(r"^--?[A-Za-z][A-Za-z-]*$")
BROWSER_PREFIX = "mcp__playwright__browser_"
GUARD_TAG = "MonkeyTesting guard"
LIMIT_RE = re.compile(r"(rate|usage|session)[ _-]?limit|limit reached|overloaded|429", re.I)


def checked_argv(args: list) -> list[str]:
    """Refuse any argument that shell quoting would change, or that looks like an unexpected option.
    Paths are passed with forward slashes (fwd()), which every OS accepts and quoting leaves alone."""
    out = [str(a) for a in args]
    if not out or not out[0]:
        raise ValueError("empty command")
    for a in out:
        if CONTROL_RE.search(a) or shlex.quote(a) != a or (a.startswith("-") and not FLAG_RE.match(a)):
            raise ValueError(f"Refusing unsafe command-line argument: {a!r}")
    return out


def quote_safe(arg: str) -> bool:
    return shlex.quote(arg) == arg


def program(name: str) -> str:
    """How to start a program found on PATH: its full forward-slash path, or, when that path has spaces
    (C:/Program Files/nodejs/node.exe, C:/Users/First Last/...), its file name, which the OS finds on PATH
    the same way shutil.which did. The bare name when it isn't installed."""
    found = shutil.which(name)
    if not found:
        return name
    full = fwd(found)
    return full if quote_safe(full) else os.path.basename(found)


def rel_arg(path, cwd) -> str:
    """A path argument relative to the child's working folder (forward slashes), so a home folder with a
    space never appears on a command line; the absolute path when that is shorter or the only option."""
    full = fwd(path)
    try:
        rel = Path(os.path.relpath(os.path.normpath(os.path.abspath(os.fspath(path))),
                                   os.path.normpath(os.path.abspath(os.fspath(cwd))))).as_posix()
    except ValueError:   # another drive on Windows
        return full
    if quote_safe(full) and len(full) <= len(rel):
        return full
    return rel if quote_safe(rel) else full


def fwd(path) -> str:
    """Absolute path with forward slashes (what settings and MCP JSON files get on every OS)."""
    return Path(os.path.normpath(os.path.abspath(os.fspath(path)))).as_posix()


def inside(base, path) -> Path:
    """Normalized absolute path, refused unless it is `base` or below it."""
    b = os.path.normpath(os.path.abspath(os.fspath(base)))
    p = os.path.normpath(os.path.abspath(os.fspath(path)))
    if p != b and not p.startswith(b + os.sep):
        raise ValueError(f"Refusing path outside {b}: {p}")
    return Path(p)


def short(s, n: int = 160) -> str:
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 1] + "…"


def parse_marker_value(raw: str):
    raw = raw.strip()
    if raw.startswith("{") or raw.startswith("["):
        try:
            return json.loads(raw)
        except ValueError:
            end = raw.rfind("}")  # tolerate trailing text after the JSON
            if end > 0:
                try:
                    return json.loads(raw[: end + 1])
                except ValueError:
                    pass
    return raw


@dataclass
class Event:
    kind: str            # tool | marker | blocked | warn | tool_error
    text: str
    name: str = ""       # marker name, or tool name
    data: object = None
    browser: bool = False


class Masker:
    """Replaces secret values (session credentials) in anything written to logs or the database."""

    def __init__(self, secrets: list[str] | None = None):
        self.secrets = sorted({s for s in (secrets or []) if s and len(s) >= 3}, key=len, reverse=True)

    def __call__(self, text: str) -> str:
        for s in self.secrets:
            text = text.replace(s, "••••••")
        return text


def summarize_tool(name: str, args: dict) -> str:
    args = args or {}
    if name.startswith(BROWSER_PREFIX):
        verb = name[len(BROWSER_PREFIX):]
        if verb == "navigate":
            return f"navigate {args.get('url', '')}"
        if verb in ("click", "hover", "drag", "select_option", "check", "uncheck"):
            return short(f"{verb} {args.get('element') or args.get('startElement') or args.get('ref', '')}")
        if verb == "type":
            return short(f"type into {args.get('element') or args.get('ref', '')}: {args.get('text', '')!s}", 140)
        if verb == "fill_form":
            names = ", ".join(str(f.get("name", "?")) for f in args.get("fields") or [])
            return short(f"fill form: {names}")
        if verb == "press_key":
            return f"press {args.get('key', '')}"
        return verb
    if name == "Bash":
        return "$ " + short(args.get("command", ""))
    if name in ("Read", "Write", "Edit"):
        return f"{name} {Path(str(args.get('file_path', ''))).name}"
    return name


class StreamParser:
    """Turns claude stream-json lines into events and running totals."""

    def __init__(self, masker: Masker | None = None):
        self.mask = masker or Masker()
        self.session_id: str | None = None
        self.result: dict | None = None
        self.rate_limited = False
        self.out_by_msg: dict[str, int] = {}
        self.input_tokens = 0
        self.last_text = ""
        self.texts: list[str] = []
        self.url = ""
        self.pending: dict[str, str] = {}   # tool_use id -> tool name

    @property
    def tokens_out(self) -> int:
        return sum(self.out_by_msg.values())

    def feed(self, obj: dict) -> list[Event]:
        events: list[Event] = []
        t = obj.get("type")
        parent = obj.get("parent_tool_use_id")
        if t == "system" and obj.get("subtype") == "init":
            self.session_id = obj.get("session_id")
            for s in obj.get("mcp_servers") or []:
                if isinstance(s, dict) and s.get("status") not in (None, "connected"):
                    events.append(Event("warn", f"MCP server {s.get('name')} is {s.get('status')}"))
        elif t == "assistant":
            msg = obj.get("message") or {}
            mid = msg.get("id") or ""
            usage = msg.get("usage") or {}
            if mid:
                self.out_by_msg[mid] = max(self.out_by_msg.get(mid, 0), int(usage.get("output_tokens") or 0))
            for block in msg.get("content") or []:
                kind = block.get("type")
                if kind == "text" and not parent:
                    text = self.mask(block.get("text") or "")
                    if text.strip():
                        self.last_text = text
                        self.texts.append(text)
                    for line in text.splitlines():
                        m = MARKER_RE.match(line)
                        if m:
                            events.append(Event("marker", short(line, 300), name=m.group(1),
                                                data=parse_marker_value(m.group(2))))
                elif kind == "tool_use":
                    name = block.get("name") or "tool"
                    args = block.get("input") or {}
                    self.pending[block.get("id") or ""] = name
                    if name == BROWSER_PREFIX + "navigate" and args.get("url"):
                        self.url = str(args["url"])
                    events.append(Event("tool", self.mask(summarize_tool(name, args)), name=name, data=args,
                                        browser=name.startswith(BROWSER_PREFIX)))
        elif t == "user":
            content = (obj.get("message") or {}).get("content")
            if isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict) or block.get("type") != "tool_result":
                        continue
                    name = self.pending.pop(block.get("tool_use_id") or "", "")
                    text = _result_text(block.get("content"))
                    m = PAGE_URL_RE.search(text)
                    if m:
                        if m.group(1) != self.url:
                            events.append(Event("page", m.group(1)))
                        self.url = m.group(1)
                    if GUARD_TAG in text:
                        events.append(Event("blocked", self.mask(short(text[text.find(GUARD_TAG):], 300)), name=name))
                    elif block.get("is_error"):
                        events.append(Event("tool_error", self.mask(short(text, 300)), name=name))
        elif t == "rate_limit_event":
            info = obj.get("rate_limit_info") or {}
            if info.get("status") not in (None, "allowed", "allowed_warning"):
                self.rate_limited = True
                events.append(Event("warn", f"Usage limit reached ({info.get('rateLimitType', 'limit')})"))
        elif t == "result":
            self.result = obj
            u = obj.get("usage") or {}
            total_out = int(u.get("output_tokens") or 0)
            if total_out > self.tokens_out:
                self.out_by_msg = {"session": total_out}
            self.input_tokens = sum(int(u.get(k) or 0) for k in
                                    ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
        return events


def _result_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(c.get("text", "")) for c in content if isinstance(c, dict))
    return ""


@dataclass
class RunResult:
    rc: int
    parser: StreamParser
    stop_reason: str | None
    stderr_tail: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        r = self.parser.result or {}
        return bool(r) and not r.get("is_error")

    @property
    def final_text(self) -> str:
        r = self.parser.result or {}
        return self.parser.mask(str(r.get("result") or "")) or self.parser.last_text

    @property
    def error(self) -> str:
        r = self.parser.result or {}
        return short(str(r.get("result") or r.get("subtype") or "") or self.stderr_tail[-600:]
                     or f"claude exited with code {self.rc}", 600)

    @property
    def rate_limited(self) -> bool:
        return not self.ok and (self.parser.rate_limited or bool(LIMIT_RE.search(self.error)))


class ClaudeRun:
    """One `claude -p` process: prompt on stdin, stream-json on stdout, events via callback."""

    def __init__(self, argv: list[str], prompt: str, cwd, env: dict, log_path,
                 on_event: Callable[[Event, "ClaudeRun"], None], masker: Masker | None = None):
        self.argv = checked_argv(argv)
        self.prompt, self.cwd, self.env, self.log_path = prompt, cwd, env, log_path
        self.on_event = on_event
        self.parser = StreamParser(masker)
        self.proc: asyncio.subprocess.Process | None = None
        self.stop_reason: str | None = None
        self.stderr_tail = ""

    def stop(self, reason: str) -> None:
        if self.stop_reason:
            return
        self.stop_reason = reason
        if self.proc and self.proc.returncode is None:
            procs.terminate_tree(self.proc.pid)
            try:
                asyncio.get_running_loop().call_later(10, self._kill)
            except RuntimeError:
                pass

    def _kill(self) -> None:
        if self.proc:
            procs.kill_running(self.proc)

    async def _read_stderr(self) -> None:
        assert self.proc and self.proc.stderr
        buf = b""
        while chunk := await self.proc.stderr.read(4096):
            buf = (buf + chunk)[-8000:]
        self.stderr_tail = self.parser.mask(buf.decode(errors="replace").strip())

    async def run(self) -> RunResult:
        log_file = os.path.normpath(os.path.abspath(os.fspath(self.log_path)))
        if not log_file.endswith(".jsonl") or not os.path.isdir(os.path.dirname(log_file)):
            raise ValueError(f"Log path must be a .jsonl file in an existing folder: {log_file}")
        self.proc = await asyncio.create_subprocess_exec(
            *[shlex.quote(a) for a in self.argv], cwd=str(self.cwd),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            limit=64 * 1024 * 1024, env=self.env, **procs.group_kwargs())
        if self.stop_reason:  # stopped while starting
            procs.terminate_tree(self.proc.pid)
        self.proc.stdin.write(self.prompt.encode("utf-8"))
        try:
            await self.proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass
        self.proc.stdin.close()
        stderr_task = asyncio.create_task(self._read_stderr())
        with open(log_file, "a", encoding="utf-8") as raw_log:
            while True:
                try:
                    line = await self.proc.stdout.readline()
                except ValueError:  # one line over the limit: skip it
                    await self.proc.stdout.read(64 * 1024 * 1024)
                    continue
                if not line:
                    break
                text = line.decode("utf-8", errors="replace")
                raw_log.write(self.parser.mask(text))
                try:
                    obj = json.loads(text)
                except ValueError:
                    continue
                for ev in self.parser.feed(obj):
                    try:
                        self.on_event(ev, self)
                    except Exception:  # a handler bug must not kill the run
                        log.exception("event handler failed")
        rc = await self.proc.wait()
        procs.reap_group(self.proc.pid)  # leftover node / chromium children of a finished run (POSIX)
        await stderr_task
        return RunResult(rc=rc, parser=self.parser, stop_reason=self.stop_reason, stderr_tail=self.stderr_tail)


SECRET_ENV = ("JIRA_API_TOKEN",)   # workers never get the Jira token


def worker_env(extra: dict | None = None) -> dict:
    """The worker's environment: ours minus the Jira token, plus the usual user tool folders on POSIX."""
    env = dict(os.environ)
    if os.name != "nt":
        more = [str(Path.home() / ".local" / "bin"), "/opt/homebrew/bin", "/usr/local/bin"]
        parts = env.get("PATH", "/usr/bin:/bin").split(os.pathsep)
        env["PATH"] = os.pathsep.join([p for p in more if p not in parts] + parts)
    for k in SECRET_ENV:
        env.pop(k, None)
    env.update(extra or {})
    return env
