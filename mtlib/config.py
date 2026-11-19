"""Settings from config.toml, with defaults for every key."""
from __future__ import annotations

import os
import shlex
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.toml"


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
    viewport: str = "1280x800"
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
    jira_labels: list[str] = field(default_factory=lambda: ["monkeytesting"])
    data_dir: Path = ROOT

    @property
    def claude_cmd(self) -> list[str]:
        return shlex.split(self.claude_bin)

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
        return ROOT

    @property
    def mcp_cli(self) -> Path:
        return self.node_root / "node_modules" / "@playwright" / "mcp" / "cli.js"

    @property
    def playwright_cli(self) -> Path:
        return self.node_root / "node_modules" / "@playwright" / "test" / "cli.js"

    @property
    def jira_token(self) -> str:
        return os.environ.get("JIRA_API_TOKEN", "")

    @property
    def jira_ready(self) -> bool:
        return bool(self.jira_base_url and self.jira_email and self.jira_project_key and self.jira_token)

    def ensure_dirs(self) -> None:
        for d in (self.db_path.parent, self.runs_dir, self.logs_dir):
            d.mkdir(parents=True, exist_ok=True)


def load(path: Path | None = None) -> Config:
    path = path or CONFIG_PATH
    raw: dict = {}
    if path.exists():
        with open(path, "rb") as fh:
            raw = tomllib.load(fh)
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
    cfg.data_dir = (path.parent / paths.get("data_dir", ".")).resolve()
    return cfg
