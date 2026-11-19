"""Deleting a session with all its data and files.

Order: mark the session `deleting` (the dispatcher no longer picks its tasks) -> remove the credentials
file first -> remove runs/s<id>/ (videos, screenshots, specs, logs) -> remove every database row.
If some files can't be removed, the rows stay and the session shows "delete incomplete" with Retry.
A running session is cancelled first; the dispatcher deletes it once its workers have stopped.
Jira tickets the session created are NOT deleted: they live outside MonkeyTesting.
"""
from __future__ import annotations

import logging
import os
import shutil
import stat
import sys
import time
from pathlib import Path

from .config import Config
from .store import SESSION_ACTIVE, Store

log = logging.getLogger("mt.deletion")
SECRET_FILES = ("secrets.env",)


def run_folder(cfg: Config, sid: int) -> Path:
    """runs/s<id>, refused unless it resolves inside runs/ (no ../ tricks, no symlink escapes)."""
    if not isinstance(sid, int) or isinstance(sid, bool) or sid <= 0:
        raise ValueError(f"bad session id: {sid!r}")
    base = Path(os.path.normpath(os.path.abspath(os.fspath(cfg.runs_dir)))).resolve()
    target = (base / f"s{sid}").resolve()
    if not target.is_relative_to(base) or target == base:
        raise ValueError(f"refusing to delete outside {base}: {target}")
    return target


def folder_size(path: Path) -> int:
    total = 0
    if not path.is_dir():
        return 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError as e:
                log.debug("size of %s: %s", f, e)
    return total


def _clear_readonly_and_retry(func, path, _exc) -> None:
    """rmtree error handler: files can be read-only (git clones on Windows); clear the bit and retry once."""
    try:
        os.chmod(path, stat.S_IWRITE | stat.S_IREAD | stat.S_IEXEC)
        func(path)
    except OSError as e:
        log.debug("still can't remove %s: %s", path, e)


def remove_tree(path: Path, attempts: int = 3) -> list[str]:
    """Removes the folder; returns what is left (empty = all gone). Retries with a short wait, because on
    Windows a file that was just written may still be locked for a moment."""
    for i in range(attempts):
        if not path.exists():
            return []
        try:
            if sys.version_info >= (3, 12):
                shutil.rmtree(path, onexc=_clear_readonly_and_retry)
            else:  # Python 3.11
                shutil.rmtree(path, onerror=_clear_readonly_and_retry)
        except OSError as e:
            log.debug("rmtree %s attempt %s: %s", path, i + 1, e)
        if not path.exists():
            return []
        time.sleep(0.5 * (i + 1))
    left = []
    for root, _dirs, files in os.walk(path):
        left += [os.path.join(root, f) for f in files]
        if len(left) > 20:
            break
    return left or [str(path)]


def remove_secrets(folder: Path) -> None:
    for name in SECRET_FILES:
        p = folder / name
        if p.is_file():
            try:
                os.chmod(p, stat.S_IWRITE | stat.S_IREAD)
                p.unlink()
            except OSError as e:
                log.warning("couldn't remove %s: %s", p, e)


def preview(cfg: Config, store: Store, sid: int) -> dict | None:
    """What a delete will remove, for the confirmation step."""
    out = store.delete_preview(sid)
    if out is None:
        return None
    out["bytes"] = folder_size(run_folder(cfg, sid))
    out["running"] = out["session"]["status"] in SESSION_ACTIVE
    return out


def request(cfg: Config, store: Store, sid: int) -> dict:
    """Deletes now if the session isn't running; otherwise cancels it and leaves the delete to the dispatcher."""
    s = store.get_session(sid)
    if not s:
        raise KeyError("no such session")
    run_folder(cfg, sid)  # validates the id before anything changes
    if s["status"] in SESSION_ACTIVE:
        store.update_session(sid, cancel_requested=1, delete_state="requested")
        store.add_event(sid, None, "dashboard", "info", "cancel & delete requested")
        return {"id": sid, "state": "cancelling", "deleted": False}
    return delete_session(cfg, store, sid)


def delete_session(cfg: Config, store: Store, sid: int) -> dict:
    folder = run_folder(cfg, sid)
    if not store.get_session(sid):
        raise KeyError("no such session")
    store.update_session(sid, delete_state="deleting")
    remove_secrets(folder)   # credentials go first, even if a later step fails
    freed = folder_size(folder)
    left = remove_tree(folder)
    if left:
        store.update_session(sid, delete_state="incomplete")
        store.add_event(sid, None, "dashboard", "error", f"delete incomplete: {len(left)} file(s) couldn't be removed")
        return {"id": sid, "state": "incomplete", "deleted": False, "left": left[:20]}
    removed = store.delete_session_rows(sid)
    log.info("deleted session %s (%s bytes, rows %s)", sid, freed, removed)
    return {"id": sid, "state": "deleted", "deleted": True, "bytes": freed, "rows": removed}


def matching(store: Store, older_than_s: float | None = None, status: str | None = None) -> list[dict]:
    """Sessions that are not running and match the filters (age by finish time, else creation time)."""
    now, out = time.time(), []
    for s in store.list_sessions(100000):
        if s["status"] in SESSION_ACTIVE:
            continue
        if status and status != "finished" and s["status"] != status:
            continue
        if older_than_s and now - float(s["finished_at"] or s["created_at"] or now) < older_than_s:
            continue
        out.append(s)
    return out
