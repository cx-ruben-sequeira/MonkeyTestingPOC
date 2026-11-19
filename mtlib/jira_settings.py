"""Jira connection set from the dashboard: URL, email and project key in the settings table (they override
[jira] in config.toml). The API token entered in the dashboard is kept in memory only, for as long as
`mt serve` runs: it is never written to disk, never stored in the database, never logged, never sent back
to the page, and workers never get it. To keep a token across restarts, put JIRA_API_TOKEN in .env.
"""
from __future__ import annotations

import os
import re
from urllib.parse import urlsplit

from .config import Config
from .store import Store

KEYS = {"jira.base_url": "jira_base_url", "jira.email": "jira_email", "jira.project_key": "jira_project_key"}
EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,255}\.[^@\s]{2,}$")
PROJECT_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,19}$")
TOKEN_RE = re.compile(r"^[A-Za-z0-9_\-=+/.:]{16,512}$")


class SettingsError(ValueError):
    pass


def apply(cfg: Config, store: Store) -> None:
    """Values saved from the dashboard win over config.toml."""
    for key, attr in KEYS.items():
        v = store.get_setting(key)
        if v is not None:
            setattr(cfg, attr, v)


def token_source(cfg: Config) -> str:
    if cfg.jira_token_memory:
        return "dashboard"
    return "environment" if os.environ.get("JIRA_API_TOKEN") else ""


def view(cfg: Config) -> dict:
    """What the page may see: everything except the token itself."""
    return {"base_url": cfg.jira_base_url, "email": cfg.jira_email, "project_key": cfg.jira_project_key,
            "token_saved": bool(cfg.jira_token), "token_from": token_source(cfg),
            "read_ready": cfg.jira_read_ready, "ready": cfg.jira_ready}


def set_token(cfg: Config, token: str) -> None:
    """Keeps the token in this process only (no file, no database)."""
    token = (token or "").strip()
    if not TOKEN_RE.match(token):
        raise SettingsError("that doesn't look like an Atlassian API token (no spaces, 16-512 characters)")
    cfg.jira_token_memory = token


def clear_token(cfg: Config) -> None:
    cfg.jira_token_memory = ""


def save(cfg: Config, store: Store, form: dict) -> dict:
    """Validates and saves the Jira connection form. An empty token field keeps the current token."""
    base = str(form.get("base_url") or "").strip().rstrip("/")
    email = str(form.get("email") or "").strip()
    project = str(form.get("project_key") or "").strip().upper()
    if base:
        p = urlsplit(base)
        if p.scheme != "https" or not p.hostname or p.path not in ("", "/") or p.query:
            raise SettingsError("Jira URL must look like https://yourcompany.atlassian.net")
        base = f"https://{p.netloc}"
    if email and not EMAIL_RE.match(email):
        raise SettingsError("that doesn't look like an email address")
    if project and not PROJECT_RE.match(project):
        raise SettingsError("project key must look like QA or AST (letters, digits, _)")
    if form.get("clear_token"):
        clear_token(cfg)
    elif form.get("token"):
        set_token(cfg, str(form["token"]))
    for key, value in (("jira.base_url", base), ("jira.email", email), ("jira.project_key", project)):
        store.set_setting(key, value)
    apply(cfg, store)
    return view(cfg)
