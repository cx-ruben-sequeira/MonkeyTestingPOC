"""`mt install` and `mt doctor`: the same setup and checks on Windows, macOS and Linux.

Programs are found with shutil.which (finds npm.cmd / claude.cmd / node.exe on Windows) and always run
from a checked argument list, never through a shell. Every check prints one line: OK, WARN (optional)
or FIX (something to do, with how to do it).
"""
from __future__ import annotations

import os
import platform
import re
import shlex
import socket
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .config import ROOT, Config
from .worker import checked_argv, program, rel_arg

MIN_PYTHON = (3, 11)
MIN_NODE = 20
SYSTEM = platform.system()   # "Windows" | "Darwin" | "Linux"


@dataclass
class Check:
    status: str      # OK | WARN | FIX
    name: str
    detail: str

    def line(self) -> str:
        return f"{self.status:<4}  {self.name:<22} {self.detail}"


def run(argv: list[str], cwd=None, timeout: int = 60) -> tuple[int, str]:
    """Runs a program from a checked argument list (no shell); returns (exit code, stdout+stderr)."""
    try:
        args = checked_argv(argv)
        out = subprocess.run([shlex.quote(a) for a in args], cwd=cwd, capture_output=True, text=True,
                             timeout=timeout, check=False, shell=False, encoding="utf-8", errors="replace")
    except (OSError, ValueError, subprocess.SubprocessError) as e:
        return 127, str(e)
    return out.returncode, (out.stdout or "") + (out.stderr or "")


def version_of(text: str) -> tuple[int, ...]:
    m = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", text or "")
    return tuple(int(x) for x in m.groups() if x is not None) if m else ()


def browsers_dir() -> Path:
    """Where Playwright keeps its browsers on this OS (or PLAYWRIGHT_BROWSERS_PATH)."""
    custom = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if custom and custom != "0":
        return Path(custom).expanduser()
    if SYSTEM == "Windows":
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "ms-playwright"
    if SYSTEM == "Darwin":
        return Path.home() / "Library" / "Caches" / "ms-playwright"
    return Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "ms-playwright"


def windows_long_paths() -> bool | None:
    """True/False from the registry on Windows, None elsewhere or when it can't be read."""
    if SYSTEM != "Windows":
        return None
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\FileSystem") as k:
            return bool(winreg.QueryValueEx(k, "LongPathsEnabled")[0])
    except OSError:
        return None


# ---------------------------------------------------------------- checks
def check_python() -> Check:
    if sys.version_info[:2] < MIN_PYTHON:
        return Check("FIX", "python", f"{platform.python_version()} is too old: install Python 3.11 or newer")
    return Check("OK", "python", f"{platform.python_version()} ({sys.executable})")


def check_program(name: str, args: list[str], fix: str, minimum: int | None = None, optional: bool = False) -> Check:
    exe = program(name)
    if exe == name:
        return Check("WARN" if optional else "FIX", name, f"not found on PATH: {fix}")
    rc, out = run([exe, *args], timeout=30)
    if rc != 0:
        return Check("WARN" if optional else "FIX", name, f"`{name} {' '.join(args)}` failed: {out.strip()[:160]}")
    ver = version_of(out)
    if minimum and (not ver or ver[0] < minimum):
        return Check("FIX", name, f"version {'.'.join(map(str, ver)) or '?'} is too old: {fix}")
    return Check("OK", name, f"{'.'.join(map(str, ver)) or out.strip()[:40]} ({exe})")


def check_gh_login() -> Check:
    gh = program("gh")
    if gh == "gh":
        return Check("WARN", "gh login", "gh isn't installed (optional today): https://cli.github.com")
    rc, out = run([gh, "auth", "status"], timeout=30)
    if rc != 0:
        return Check("WARN", "gh login", "not logged in (optional today): run `gh auth login`")
    m = re.search(r"account (\S+)", out)
    return Check("OK", "gh login", f"logged in{(' as ' + m.group(1)) if m else ''}")


def check_node_packages(cfg: Config) -> Check:
    missing = [p.parent.parent.name + "/" + p.parent.name for p in (cfg.mcp_cli, cfg.playwright_cli) if not p.is_file()]
    if missing:
        return Check("FIX", "playwright packages", f"missing {', '.join(missing)} in runner/: run `mt install`")
    return Check("OK", "playwright packages", str(cfg.node_root / "node_modules"))


def check_browser() -> Check:
    d = browsers_dir()
    found = sorted(p.name for p in d.glob("chromium*")) if d.is_dir() else []
    if not found:
        return Check("FIX", "chromium browser", f"not installed in {d}: run `mt install`")
    return Check("OK", "chromium browser", f"{', '.join(found[-2:])} in {d}")


def check_data_dir(cfg: Config) -> Check:
    try:
        cfg.ensure_dirs()
        with tempfile.NamedTemporaryFile(dir=cfg.runs_dir, prefix=".mt-write-check-", delete=True) as fh:
            fh.write(b"ok")
    except OSError as e:
        return Check("FIX", "data folder", f"{cfg.data_dir} isn't writable: {e} (set MT_DATA_DIR to another folder)")
    return Check("OK", "data folder", f"{cfg.data_dir} (writable)")


def check_port(cfg: Config) -> Check:
    s = socket.socket(socket.AF_INET6 if ":" in cfg.host else socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind((cfg.host, cfg.port))
    except OSError:
        return Check("WARN", "server port", f"{cfg.host}:{cfg.port} is in use (a running `mt serve`? or set [server] port)")
    finally:
        s.close()
    return Check("OK", "server port", f"{cfg.host}:{cfg.port} is free")


def check_jira(cfg: Config) -> Check:
    if not (cfg.jira_base_url or cfg.jira_project_key):
        return Check("OK", "jira", "not configured (optional)")
    if not cfg.jira_token:
        return Check("WARN", "jira", "[jira] is set but JIRA_API_TOKEN isn't (environment or .env)")
    return Check("OK", "jira", f"{cfg.jira_base_url} project {cfg.jira_project_key}")


def check_checkout() -> Check:
    need = [ROOT / "web" / "index.html", ROOT / "hooks" / "guard.py", ROOT / "runner" / "package.json"]
    missing = [str(p.relative_to(ROOT)) for p in need if not p.is_file()]
    if missing:
        return Check("FIX", "checkout", f"{ROOT} lacks {', '.join(missing)}: install with `pipx install -e .` "
                                        "from the repository folder (an editable install)")
    return Check("OK", "checkout", str(ROOT))


def check_long_paths() -> Check | None:
    lp = windows_long_paths()
    if lp is False:
        return Check("WARN", "windows long paths", "off: run folders can hit the 260-character limit. Enable it "
                                                     "(admin PowerShell): New-ItemProperty -Path "
                                                     "HKLM:\\SYSTEM\\CurrentControlSet\\Control\\FileSystem "
                                                     "-Name LongPathsEnabled -Value 1 -PropertyType DWORD -Force")
    return Check("OK", "windows long paths", "enabled") if lp else None


def doctor(cfg: Config, out=print) -> int:
    """Prints one line per check; returns 0 when nothing needs fixing."""
    checks = [
        check_checkout(), check_python(),
        check_program("node", ["--version"], f"install Node {MIN_NODE}+ from https://nodejs.org", MIN_NODE),
        check_program("npm", ["--version"], "comes with Node.js (https://nodejs.org)"),
        check_program("claude", ["--version"], "install Claude Code (https://docs.claude.com/claude-code) and log in"),
        check_program("git", ["--version"], "install git: https://git-scm.com"),
        check_gh_login(), check_node_packages(cfg), check_browser(), check_data_dir(cfg), check_port(cfg),
        check_jira(cfg), check_long_paths(),
    ]
    checks = [c for c in checks if c]
    out(f"MonkeyTesting doctor on {SYSTEM} {platform.release()} ({platform.machine()})")
    for c in checks:
        out(c.line())
    fixes = sum(1 for c in checks if c.status == "FIX")
    out("All good." if not fixes else f"{fixes} thing(s) to fix (lines marked FIX).")
    return 1 if fixes else 0


# ---------------------------------------------------------------- install
def install(cfg: Config, with_deps: bool | None = None, out=print) -> int:
    """npm ci in runner/, the Playwright Chromium build (plus its system libraries on Linux), data folders,
    then `mt doctor`. Safe to run again."""
    if sys.version_info[:2] < MIN_PYTHON:
        out(check_python().line())
        return 1
    node, npm = program("node"), program("npm")
    if node == "node" or npm == "npm":
        out(check_program("node", ["--version"], f"install Node {MIN_NODE}+ from https://nodejs.org", MIN_NODE).line())
        return 1
    runner = cfg.node_root
    out(f"Installing the pinned Playwright packages in {runner} ...")
    lock = runner / "package-lock.json"
    rc, text = run([npm, "ci" if lock.is_file() else "install", "--no-audit", "--no-fund"], cwd=runner, timeout=900)
    if rc != 0:
        out(text[-2000:])
        out("FIX   npm failed (see above). Check your network/proxy and try again.")
        return 1
    if with_deps is None:
        with_deps = SYSTEM == "Linux"
    args = [node, rel_arg(cfg.playwright_cli, runner), "install"] + (["--with-deps"] if with_deps else []) + ["chromium"]
    out("Installing the Chromium build Playwright uses"
        + (" and its system libraries (may ask for sudo)" if with_deps else "") + " ...")
    rc, text = run(args, cwd=runner, timeout=1800)
    if rc != 0:
        out(text[-2000:])
        hint = (" On Linux without sudo, run `mt install --no-deps` and have an admin install the libraries "
                "(`npx playwright install-deps chromium`).") if with_deps else ""
        out("FIX   Playwright couldn't install Chromium (see above)." + hint)
        return 1
    cfg.ensure_dirs()
    out("")
    return doctor(cfg, out)
