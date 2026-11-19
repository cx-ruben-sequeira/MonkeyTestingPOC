"""Settings from config.toml, with defaults for every key.

Secrets (JIRA_API_TOKEN) come from the environment, or from a git-ignored `.env` file next to
config.toml, so there are no per-shell `export` / `$env:` differences between operating systems.
MT_DATA_DIR (environment or .env) moves data/, runs/ and logs/ elsewhere.
"""
from __future__ import annotations

import os
import shlex
import shutil
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.toml"
ENV_KEYS = ("JIRA_API_TOKEN", "MT_DATA_DIR")


def clean_path(raw, base: Path | None = None) -> str:
    """Absolute, normalized path; relative values resolve against `base` (default: the repo root)."""
    raw = os.path.expanduser(os.fspath(raw))
    if not os.path.isabs(raw):
        raw = os.path.join(os.fspath(base or ROOT), raw)
    return os.path.normpath(os.path.abspath(raw))


def load_dotenv(path) -> None:
    """KEY=value lines from .env into os.environ (only known keys, never overriding the real environment)."""
    full = os.path.normpath(os.path.abspath(os.fspath(path)))
    if os.path.basename(full) != ".env" or not os.path.isfile(full):
        return
    with open(full, "r", encoding="utf-8-sig") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k.startswith("export "):
                k = k[len("export "):].strip()
            if k in ENV_KEYS and k not in os.environ:
                os.environ[k] = v


@dataclass
class Config:
    host: str = "127.0.0.1"
    port: int = 8766
    max_workers: int = 3
    claude_bin: str = "claude"
    model: str = ""
    effort: str = ""
    max_budget_usd: float = 0.0
    max_attempts: int = 2
    browser: str = "chromium"
    headless: bool = True
    viewport: str = "1920x1080"
    action_timeout_ms: int = 8000
    explorer_action_share: float = 0.25
    max_charters: int = 8
    loop_repeat_limit: int = 4
    max_rounds: int = 6
    repro_timeout_s: int = 900
    stop_grace_s: int = 120
    jira_base_url: str = ""
    jira_email: str = ""
    jira_project_key: str = ""
    jira_issue_type: str = "Bug"
    jira_labels: list[str] = None
    jira_token_memory: str = field(default="", repr=False)   # set from the dashboard; never on disk or in repr()
    data_dir: Path = ROOT

    def __post_init__(self):
        if self.jira_labels is None:
            self.jira_labels = ["monkeytesting"]

    @property
    def claude_cmd(self) -> list[str]:
        """The claude command as an argument list; a bare program name is looked up on PATH
        (shutil.which also finds claude.cmd / claude.exe on Windows)."""
        parts = shlex.split(self.claude_bin, posix=os.name != "nt")
        parts = [p.strip('"') for p in parts]
        if parts and not os.path.isabs(parts[0]):
            found = shutil.which(parts[0])
            if found:
                parts[0] = found
        return parts

    @property
    def db_path(self) -> Path:
        return self.data_dir / "data" / "mt.db"

    @property
    def runs_dir(self) -> Path:
        return self.data_dir / "runs"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def node_root(self) -> Path:
        """Folder holding package.json and node_modules with the pinned Playwright packages."""
        return ROOT / "runner"

    @property
    def mcp_cli(self) -> Path:
        return self.node_root / "node_modules" / "@playwright" / "mcp" / "cli.js"

    @property
    def playwright_cli(self) -> Path:
        return self.node_root / "node_modules" / "@playwright" / "test" / "cli.js"

    @property
    def jira_token(self) -> str:
        """The token entered in the dashboard (memory only), else JIRA_API_TOKEN (environment or .env)."""
        return self.jira_token_memory or os.environ.get("JIRA_API_TOKEN", "")

    @property
    def jira_read_ready(self) -> bool:
        """Enough to read tickets (no project key needed)."""
        return bool(self.jira_base_url and self.jira_email and self.jira_token)

    @property
    def jira_ready(self) -> bool:
        """Enough to create bugs."""
        return bool(self.jira_read_ready and self.jira_project_key)

    def ensure_dirs(self) -> None:
        for d in (self.db_path.parent, self.runs_dir, self.logs_dir):
            d.mkdir(parents=True, exist_ok=True)


def data_dir_from(env_value: str | None, config_dir: Path, configured: str) -> Path:
    """MT_DATA_DIR when set, else [paths] data_dir relative to the config file. Never an existing file."""
    path = clean_path(env_value) if env_value else clean_path(configured or ".", config_dir)
    if os.path.isfile(path):
        raise ValueError(f"data folder is a file: {path}")
    return Path(path)


def read_toml(path) -> dict:
    """The parsed config file, or {} when there is none. Only .toml files are read."""
    full = os.path.normpath(os.path.abspath(os.fspath(path)))
    if not full.endswith(".toml"):
        raise ValueError(f"config must be a .toml file: {full}")
    if not os.path.isfile(full):
        return {}
    with open(full, "rb") as fh:
        return tomllib.load(fh)


def load(path: Path | None = None) -> Config:
    path = Path(clean_path(path or CONFIG_PATH))
    load_dotenv(path.parent / ".env")
    if path.parent != ROOT:
        load_dotenv(ROOT / ".env")
    raw = read_toml(path)
    s, w, p = raw.get("server", {}), raw.get("workers", {}), raw.get("playwright", {})
    lim, j, paths = raw.get("limits", {}), raw.get("jira", {}), raw.get("paths", {})
    cfg = Config()
    cfg.host = s.get("host", cfg.host)
    cfg.port = int(s.get("port", cfg.port))
    cfg.max_workers = max(1, min(10, int(w.get("max_workers", cfg.max_workers))))
    cfg.claude_bin = w.get("claude_bin", cfg.claude_bin)
    cfg.model = w.get("model", cfg.model) or ""
    cfg.effort = w.get("effort", cfg.effort) or ""
    cfg.max_budget_usd = float(w.get("max_budget_usd", cfg.max_budget_usd) or 0)
    cfg.max_attempts = max(1, int(w.get("max_attempts", cfg.max_attempts)))
    cfg.browser = p.get("browser", cfg.browser)
    cfg.headless = bool(p.get("headless", cfg.headless))
    cfg.viewport = p.get("viewport", cfg.viewport)
    cfg.action_timeout_ms = int(p.get("action_timeout_ms", cfg.action_timeout_ms))
    cfg.explorer_action_share = float(lim.get("explorer_action_share", cfg.explorer_action_share))
    cfg.max_charters = int(lim.get("max_charters", cfg.max_charters))
    cfg.loop_repeat_limit = int(lim.get("loop_repeat_limit", cfg.loop_repeat_limit))
    cfg.max_rounds = int(lim.get("max_rounds", cfg.max_rounds))
    cfg.repro_timeout_s = int(lim.get("repro_timeout_s", cfg.repro_timeout_s))
    cfg.stop_grace_s = int(lim.get("stop_grace_s", cfg.stop_grace_s))
    cfg.jira_base_url = (j.get("base_url") or "").rstrip("/")
    cfg.jira_email = j.get("email", "") or ""
    cfg.jira_project_key = j.get("project_key", "") or ""
    cfg.jira_issue_type = j.get("issue_type", cfg.jira_issue_type) or "Bug"
    cfg.jira_labels = list(j.get("labels", cfg.jira_labels) or [])
    cfg.data_dir = data_dir_from(os.environ.get("MT_DATA_DIR"), path.parent, str(paths.get("data_dir", ".")))
    return cfg
