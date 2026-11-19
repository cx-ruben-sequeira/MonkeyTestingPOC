"""Creating sessions from the dashboard form or the CLI: validation, guard rules, credentials file."""
from __future__ import annotations

import json
import os
import re
from urllib.parse import urlsplit

from .config import Config
from .store import Store

MODES = ("random", "ai", "hybrid")
BROWSERS = ("chromium", "firefox", "webkit", "chrome", "msedge")
TEST_HOST_RE = re.compile(r"(^|[.\-])(staging|stage|stg|qa|test|testing|dev|develop|sandbox|uat|preview|demo|local)"
                          r"([.\-\d]|$)|^localhost$|\.localhost$|\.test$|\.local$", re.I)
LOOPBACK = {"localhost", "127.0.0.1", "::1", "[::1]"}


class FormError(ValueError):
    pass


def origin(url: str) -> str:
    p = urlsplit(url)
    return f"{p.scheme}://{p.netloc}".lower()


def looks_like_test_host(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return host in LOOPBACK or bool(TEST_HOST_RE.search(host))


def _int(form: dict, key: str, default: int, lo: int, hi: int) -> int:
    try:
        v = int(form.get(key) if form.get(key) not in (None, "") else default)
    except (TypeError, ValueError):
        raise FormError(f"{key} must be a number")
    if not lo <= v <= hi:
        raise FormError(f"{key} must be between {lo} and {hi}")
    return v


def _url(raw: str, field: str) -> str:
    raw = (raw or "").strip()
    p = urlsplit(raw)
    if p.scheme not in ("http", "https") or not p.netloc:
        raise FormError(f"{field} must be a full http(s) URL")
    return raw


def create(cfg: Config, store: Store, form: dict) -> dict:
    """Validates the New session form and queues the session. Returns {"id": ..., "warning": ...}."""
    url = _url(form.get("url"), "URL")
    mode = str(form.get("mode") or "hybrid").lower()
    if mode not in MODES:
        raise FormError(f"mode must be one of {', '.join(MODES)}")
    duration = _int(form, "duration_min", 20, 1, 480)
    max_actions = _int(form, "max_actions", 300, 10, 10000)
    repeats = _int(form, "repeat_count", 5, 1, 20)
    browser = str(form.get("browser") or cfg.browser).lower()
    if browser not in BROWSERS:
        raise FormError(f"browser must be one of {', '.join(BROWSERS)}")
    viewport = str(form.get("viewport") or cfg.viewport)
    if not re.match(r"^\d{3,4}x\d{3,4}$", viewport):
        raise FormError("viewport must look like 1280x800")
    extra = []
    for o in re.split(r"[\s,;]+", str(form.get("extra_origins") or "")):
        if o:
            extra.append(origin(_url(o, "extra origin")))
    login_url = (form.get("login_url") or "").strip()
    if login_url:
        login_url = _url(login_url, "login URL")
        if origin(login_url) not in [origin(url)] + extra:
            extra.append(origin(login_url))
    user, password = str(form.get("username") or ""), str(form.get("password") or "")
    if any(c in user + password for c in "\r\n"):
        raise FormError("credentials can't contain line breaks")
    warning = None
    if not looks_like_test_host(url):
        warning = (f"{urlsplit(url).hostname} doesn't look like a QA/staging host "
                   "(no staging, qa, test, dev, sandbox or localhost label).")
        if not form.get("confirm_non_test"):
            raise FormError(warning + " Tick 'This is a test environment' to start anyway.")
    sid = store.add_session(
        name=(str(form.get("name") or "").strip()[:120] or urlsplit(url).netloc), url=url, origin=origin(url),
        mode=mode, duration_min=duration, max_actions=max_actions, repeat_count=repeats, browser=browser,
        viewport=viewport, instructions=str(form.get("instructions") or "")[:4000], allowed_origins=extra,
        allow_destructive=1 if form.get("allow_destructive") else 0, login_url=login_url or None,
        has_auth=1 if (user or password) else 0, status="queued")
    write_session_files(cfg, sid, [origin(url)] + extra, bool(form.get("allow_destructive")), user, password)
    store.add_event(sid, None, "dashboard", "info", f"session queued: {url}, {mode}, {duration} min, N={repeats}")
    return {"id": sid, "warning": warning}


def write_session_files(cfg: Config, sid: int, origins: list[str], allow_destructive: bool,
                        user: str, password: str) -> None:
    from .dispatcher import session_dir
    base = os.path.normpath(os.path.abspath(os.fspath(session_dir(cfg, sid))))
    guard = os.path.normpath(os.path.join(base, "guard.json"))
    if os.path.dirname(guard) != base:
        raise ValueError("bad guard path")
    with open(guard, "w", encoding="utf-8") as fh:
        json.dump({"origins": origins, "allow_destructive": allow_destructive}, fh)
    os.chmod(guard, 0o600)
    if user or password:
        secrets = os.path.normpath(os.path.join(base, "secrets.env"))
        if os.path.dirname(secrets) != base:
            raise ValueError("bad secrets path")
        fd = os.open(secrets, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.chmod(secrets, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(f"MT_USERNAME={user}\nMT_PASSWORD={password}\n")
