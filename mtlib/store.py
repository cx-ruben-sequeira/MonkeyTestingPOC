"""SQLite state shared by the orchestrator, the CLI and the dashboard server.

Every statement is a fixed prepared statement written out in full below: values are always bound
parameters and no SQL text is built at runtime. Updates use one statement per table in which each
column is only changed when its `u_<column>` flag is set.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

SESSION_ACTIVE = ("queued", "running", "reporting")
TASK_ACTIVE = ("queued", "running")
SEVERITIES = ("critical", "high", "medium", "low")
PRIORITY = {"repro": 30, "explore": 20, "test": 10, "report": 5}

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT,
    url TEXT NOT NULL,
    origin TEXT NOT NULL,
    mode TEXT NOT NULL,
    duration_min INTEGER NOT NULL,
    max_actions INTEGER NOT NULL,
    repeat_count INTEGER NOT NULL,
    browser TEXT,
    viewport TEXT,
    instructions TEXT NOT NULL DEFAULT '',
    allowed_origins TEXT NOT NULL DEFAULT '[]',
    allow_destructive INTEGER NOT NULL DEFAULT 0,
    login_url TEXT,
    has_auth INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    phase TEXT,
    seed INTEGER,
    rounds INTEGER NOT NULL DEFAULT 0,
    actions_used INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    started_at REAL,
    deadline REAL,
    budget_hit_at REAL,
    finished_at REAL,
    report_path TEXT,
    summary TEXT,
    error TEXT
);
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 0,
    title TEXT,
    charter TEXT NOT NULL DEFAULT '{}',
    suspect_id INTEGER,
    attempts INTEGER NOT NULL DEFAULT 0,
    worker_slot INTEGER,
    step_name TEXT,
    current_action TEXT,
    actions INTEGER NOT NULL DEFAULT 0,
    action_budget INTEGER,
    tokens_out INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL,
    claude_session TEXT,
    screenshot TEXT,
    result TEXT,
    error TEXT,
    stop_reason TEXT,
    created_at REAL NOT NULL,
    started_at REAL,
    finished_at REAL
);
CREATE INDEX IF NOT EXISTS tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS tasks_session ON tasks(session_id);
CREATE TABLE IF NOT EXISTS pages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL,
    url TEXT NOT NULL,
    title TEXT,
    kind TEXT,
    visited INTEGER NOT NULL DEFAULT 0,
    components TEXT NOT NULL DEFAULT '[]',
    first_seen REAL NOT NULL,
    UNIQUE(session_id, url)
);
CREATE TABLE IF NOT EXISTS edges (
    session_id INTEGER NOT NULL,
    src TEXT NOT NULL,
    dst TEXT NOT NULL,
    label TEXT,
    UNIQUE(session_id, src, dst)
);
CREATE TABLE IF NOT EXISTS suspects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL,
    task_id INTEGER,
    fingerprint TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    steps TEXT NOT NULL DEFAULT '[]',
    expected TEXT NOT NULL DEFAULT '',
    actual TEXT NOT NULL DEFAULT '',
    severity TEXT NOT NULL DEFAULT 'medium',
    confidence REAL,
    url TEXT,
    evidence TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'pending',
    reason TEXT,
    runs_total INTEGER,
    runs_failed INTEGER,
    runs TEXT NOT NULL DEFAULT '[]',
    spec_path TEXT,
    video_path TEXT,
    screenshots TEXT NOT NULL DEFAULT '[]',
    duplicate_of INTEGER,
    jira_key TEXT,
    jira_url TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE(session_id, fingerprint)
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER,
    task_id INTEGER,
    ts REAL NOT NULL,
    source TEXT NOT NULL,
    kind TEXT NOT NULL,
    text TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_session ON events(session_id);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    session_id INTEGER,
    task_id INTEGER,
    cost_usd REAL NOT NULL DEFAULT 0,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0
);
"""

# ---------------------------------------------------------------- columns and their defaults
SESSION_DEFAULTS = {
    "name": None, "url": None, "origin": None, "mode": None, "duration_min": None, "max_actions": None,
    "repeat_count": None, "browser": None, "viewport": None, "instructions": "", "allowed_origins": "[]",
    "allow_destructive": 0, "login_url": None, "has_auth": 0, "status": "queued", "phase": None, "seed": None,
    "rounds": 0, "actions_used": 0, "cost_usd": 0, "cancel_requested": 0, "created_at": None,
    "started_at": None, "deadline": None, "budget_hit_at": None, "finished_at": None, "report_path": None,
    "summary": None, "error": None,
}
TASK_DEFAULTS = {
    "session_id": None, "kind": None, "status": "queued", "priority": 0, "title": None, "charter": "{}",
    "suspect_id": None, "attempts": 0, "worker_slot": None, "step_name": None, "current_action": None,
    "actions": 0, "action_budget": None, "tokens_out": 0, "cost_usd": None, "claude_session": None,
    "screenshot": None, "result": None, "error": None, "stop_reason": None, "created_at": None,
    "started_at": None, "finished_at": None,
}
SUSPECT_DEFAULTS = {
    "session_id": None, "task_id": None, "fingerprint": None, "title": None, "description": "", "steps": "[]",
    "expected": "", "actual": "", "severity": "medium", "confidence": None, "url": None, "evidence": "[]",
    "status": "pending", "reason": None, "runs_total": None, "runs_failed": None, "runs": "[]",
    "spec_path": None, "video_path": None, "screenshots": "[]", "duplicate_of": None, "jira_key": None,
    "jira_url": None, "created_at": None, "updated_at": None,
}
JSON_COLS = {"allowed_origins", "charter", "components", "steps", "evidence", "runs", "screenshots"}

# ---------------------------------------------------------------- fixed statements
INSERT_SESSION = """
INSERT INTO sessions (
    name, url, origin, mode, duration_min, max_actions, repeat_count, browser, viewport,
    instructions, allowed_origins, allow_destructive, login_url, has_auth, status, phase, seed,
    rounds, actions_used, cost_usd, cancel_requested, created_at, started_at, deadline,
    budget_hit_at, finished_at, report_path, summary, error)
VALUES (
    :name, :url, :origin, :mode, :duration_min, :max_actions, :repeat_count, :browser, :viewport,
    :instructions, :allowed_origins, :allow_destructive, :login_url, :has_auth, :status, :phase,
    :seed, :rounds, :actions_used, :cost_usd, :cancel_requested, :created_at, :started_at,
    :deadline, :budget_hit_at, :finished_at, :report_path, :summary, :error)
"""
UPDATE_SESSION = """
UPDATE sessions SET
    name = CASE WHEN :u_name THEN :name ELSE name END,
    url = CASE WHEN :u_url THEN :url ELSE url END,
    origin = CASE WHEN :u_origin THEN :origin ELSE origin END,
    mode = CASE WHEN :u_mode THEN :mode ELSE mode END,
    duration_min = CASE WHEN :u_duration_min THEN :duration_min ELSE duration_min END,
    max_actions = CASE WHEN :u_max_actions THEN :max_actions ELSE max_actions END,
    repeat_count = CASE WHEN :u_repeat_count THEN :repeat_count ELSE repeat_count END,
    browser = CASE WHEN :u_browser THEN :browser ELSE browser END,
    viewport = CASE WHEN :u_viewport THEN :viewport ELSE viewport END,
    instructions = CASE WHEN :u_instructions THEN :instructions ELSE instructions END,
    allowed_origins = CASE WHEN :u_allowed_origins THEN :allowed_origins ELSE allowed_origins END,
    allow_destructive = CASE WHEN :u_allow_destructive THEN :allow_destructive ELSE allow_destructive END,
    login_url = CASE WHEN :u_login_url THEN :login_url ELSE login_url END,
    has_auth = CASE WHEN :u_has_auth THEN :has_auth ELSE has_auth END,
    status = CASE WHEN :u_status THEN :status ELSE status END,
    phase = CASE WHEN :u_phase THEN :phase ELSE phase END,
    seed = CASE WHEN :u_seed THEN :seed ELSE seed END,
    rounds = CASE WHEN :u_rounds THEN :rounds ELSE rounds END,
    actions_used = CASE WHEN :u_actions_used THEN :actions_used ELSE actions_used END,
    cost_usd = CASE WHEN :u_cost_usd THEN :cost_usd ELSE cost_usd END,
    cancel_requested = CASE WHEN :u_cancel_requested THEN :cancel_requested ELSE cancel_requested END,
    created_at = CASE WHEN :u_created_at THEN :created_at ELSE created_at END,
    started_at = CASE WHEN :u_started_at THEN :started_at ELSE started_at END,
    deadline = CASE WHEN :u_deadline THEN :deadline ELSE deadline END,
    budget_hit_at = CASE WHEN :u_budget_hit_at THEN :budget_hit_at ELSE budget_hit_at END,
    finished_at = CASE WHEN :u_finished_at THEN :finished_at ELSE finished_at END,
    report_path = CASE WHEN :u_report_path THEN :report_path ELSE report_path END,
    summary = CASE WHEN :u_summary THEN :summary ELSE summary END,
    error = CASE WHEN :u_error THEN :error ELSE error END
WHERE id = :id
"""
INSERT_TASK = """
INSERT INTO tasks (
    session_id, kind, status, priority, title, charter, suspect_id, attempts, worker_slot,
    step_name, current_action, actions, action_budget, tokens_out, cost_usd, claude_session,
    screenshot, result, error, stop_reason, created_at, started_at, finished_at)
VALUES (
    :session_id, :kind, :status, :priority, :title, :charter, :suspect_id, :attempts, :worker_slot,
    :step_name, :current_action, :actions, :action_budget, :tokens_out, :cost_usd, :claude_session,
    :screenshot, :result, :error, :stop_reason, :created_at, :started_at, :finished_at)
"""
UPDATE_TASK = """
UPDATE tasks SET
    session_id = CASE WHEN :u_session_id THEN :session_id ELSE session_id END,
    kind = CASE WHEN :u_kind THEN :kind ELSE kind END,
    status = CASE WHEN :u_status THEN :status ELSE status END,
    priority = CASE WHEN :u_priority THEN :priority ELSE priority END,
    title = CASE WHEN :u_title THEN :title ELSE title END,
    charter = CASE WHEN :u_charter THEN :charter ELSE charter END,
    suspect_id = CASE WHEN :u_suspect_id THEN :suspect_id ELSE suspect_id END,
    attempts = CASE WHEN :u_attempts THEN :attempts ELSE attempts END,
    worker_slot = CASE WHEN :u_worker_slot THEN :worker_slot ELSE worker_slot END,
    step_name = CASE WHEN :u_step_name THEN :step_name ELSE step_name END,
    current_action = CASE WHEN :u_current_action THEN :current_action ELSE current_action END,
    actions = CASE WHEN :u_actions THEN :actions ELSE actions END,
    action_budget = CASE WHEN :u_action_budget THEN :action_budget ELSE action_budget END,
    tokens_out = CASE WHEN :u_tokens_out THEN :tokens_out ELSE tokens_out END,
    cost_usd = CASE WHEN :u_cost_usd THEN :cost_usd ELSE cost_usd END,
    claude_session = CASE WHEN :u_claude_session THEN :claude_session ELSE claude_session END,
    screenshot = CASE WHEN :u_screenshot THEN :screenshot ELSE screenshot END,
    result = CASE WHEN :u_result THEN :result ELSE result END,
    error = CASE WHEN :u_error THEN :error ELSE error END,
    stop_reason = CASE WHEN :u_stop_reason THEN :stop_reason ELSE stop_reason END,
    created_at = CASE WHEN :u_created_at THEN :created_at ELSE created_at END,
    started_at = CASE WHEN :u_started_at THEN :started_at ELSE started_at END,
    finished_at = CASE WHEN :u_finished_at THEN :finished_at ELSE finished_at END
WHERE id = :id
"""
INSERT_SUSPECT = """
INSERT INTO suspects (
    session_id, task_id, fingerprint, title, description, steps, expected, actual, severity,
    confidence, url, evidence, status, reason, runs_total, runs_failed, runs, spec_path,
    video_path, screenshots, duplicate_of, jira_key, jira_url, created_at, updated_at)
VALUES (
    :session_id, :task_id, :fingerprint, :title, :description, :steps, :expected, :actual,
    :severity, :confidence, :url, :evidence, :status, :reason, :runs_total, :runs_failed, :runs,
    :spec_path, :video_path, :screenshots, :duplicate_of, :jira_key, :jira_url, :created_at,
    :updated_at)
"""
UPDATE_SUSPECT = """
UPDATE suspects SET
    session_id = CASE WHEN :u_session_id THEN :session_id ELSE session_id END,
    task_id = CASE WHEN :u_task_id THEN :task_id ELSE task_id END,
    fingerprint = CASE WHEN :u_fingerprint THEN :fingerprint ELSE fingerprint END,
    title = CASE WHEN :u_title THEN :title ELSE title END,
    description = CASE WHEN :u_description THEN :description ELSE description END,
    steps = CASE WHEN :u_steps THEN :steps ELSE steps END,
    expected = CASE WHEN :u_expected THEN :expected ELSE expected END,
    actual = CASE WHEN :u_actual THEN :actual ELSE actual END,
    severity = CASE WHEN :u_severity THEN :severity ELSE severity END,
    confidence = CASE WHEN :u_confidence THEN :confidence ELSE confidence END,
    url = CASE WHEN :u_url THEN :url ELSE url END,
    evidence = CASE WHEN :u_evidence THEN :evidence ELSE evidence END,
    status = CASE WHEN :u_status THEN :status ELSE status END,
    reason = CASE WHEN :u_reason THEN :reason ELSE reason END,
    runs_total = CASE WHEN :u_runs_total THEN :runs_total ELSE runs_total END,
    runs_failed = CASE WHEN :u_runs_failed THEN :runs_failed ELSE runs_failed END,
    runs = CASE WHEN :u_runs THEN :runs ELSE runs END,
    spec_path = CASE WHEN :u_spec_path THEN :spec_path ELSE spec_path END,
    video_path = CASE WHEN :u_video_path THEN :video_path ELSE video_path END,
    screenshots = CASE WHEN :u_screenshots THEN :screenshots ELSE screenshots END,
    duplicate_of = CASE WHEN :u_duplicate_of THEN :duplicate_of ELSE duplicate_of END,
    jira_key = CASE WHEN :u_jira_key THEN :jira_key ELSE jira_key END,
    jira_url = CASE WHEN :u_jira_url THEN :jira_url ELSE jira_url END,
    created_at = CASE WHEN :u_created_at THEN :created_at ELSE created_at END,
    updated_at = CASE WHEN :u_updated_at THEN :updated_at ELSE updated_at END
WHERE id = :id
"""

SELECT_SESSION = "SELECT * FROM sessions WHERE id = ?"
LIST_SESSIONS = "SELECT * FROM sessions ORDER BY id DESC LIMIT ?"
ADD_ACTIONS = "UPDATE sessions SET actions_used = actions_used + ? WHERE id = ?"
SELECT_ACTIONS = "SELECT actions_used FROM sessions WHERE id = ?"
SELECT_TASK = "SELECT * FROM tasks WHERE id = ?"
TASKS_FOR = "SELECT * FROM tasks WHERE session_id = ? ORDER BY id"
RUNNING_TASKS = "SELECT * FROM tasks WHERE status = 'running' ORDER BY worker_slot"
NEXT_TASK = ("SELECT t.* FROM tasks t JOIN sessions s ON s.id = t.session_id "
             "WHERE t.status = 'queued' AND s.status IN ('running', 'reporting') AND s.cancel_requested = 0 "
             "ORDER BY t.priority DESC, t.id ASC LIMIT 1")
CLAIM_TASK = "UPDATE tasks SET status = 'running', started_at = ?, attempts = attempts + 1 WHERE id = ?"
RECOVER_TASKS = ("UPDATE tasks SET status = 'queued', worker_slot = NULL, current_action = NULL "
                 "WHERE status = 'running'")
SELECT_PAGE = "SELECT * FROM pages WHERE session_id = ? AND url = ?"
INSERT_PAGE = ("INSERT INTO pages(session_id, url, title, kind, visited, components, first_seen) "
               "VALUES(?, ?, ?, ?, ?, ?, ?)")
UPDATE_PAGE = ("UPDATE pages SET title = COALESCE(?, title), kind = COALESCE(?, kind), "
               "visited = MAX(visited, ?), components = ? WHERE id = ?")
INSERT_EDGE = "INSERT OR IGNORE INTO edges(session_id, src, dst, label) VALUES(?, ?, ?, ?)"
LIST_PAGES = "SELECT * FROM pages WHERE session_id = ? ORDER BY id"
LIST_EDGES = "SELECT src, dst, label FROM edges WHERE session_id = ?"
SELECT_SUSPECT = "SELECT * FROM suspects WHERE id = ?"
LIST_SUSPECTS = ("SELECT * FROM suspects WHERE (? IS NULL OR session_id = ?) AND (? IS NULL OR status = ?) "
                 "ORDER BY id DESC")
INSERT_EVENT = "INSERT INTO events(session_id, task_id, ts, source, kind, text) VALUES(?, ?, ?, ?, ?, ?)"
LIST_EVENTS = "SELECT * FROM events WHERE (? IS NULL OR session_id = ?) ORDER BY id DESC LIMIT ?"
INSERT_USAGE = ("INSERT INTO usage(ts, session_id, task_id, cost_usd, input_tokens, output_tokens) "
                "VALUES(?, ?, ?, ?, ?, ?)")
ADD_SESSION_COST = "UPDATE sessions SET cost_usd = cost_usd + ? WHERE id = ?"
USAGE_TOTAL = ("SELECT COALESCE(SUM(cost_usd), 0) c, COALESCE(SUM(output_tokens), 0) o, "
               "COALESCE(SUM(input_tokens), 0) i FROM usage")
TASKS_BY_STATUS = "SELECT * FROM tasks WHERE session_id = ? AND status = ? ORDER BY id"
CANCEL_QUEUED_KIND = ("UPDATE tasks SET status = 'cancelled', finished_at = ?, stop_reason = ? "
                      "WHERE session_id = ? AND status = 'queued' AND kind = ?")
CANCEL_QUEUED_ALL = ("UPDATE tasks SET status = 'cancelled', finished_at = ?, stop_reason = ? "
                     "WHERE session_id = ? AND status = 'queued'")
COUNT_SESSIONS = "SELECT COUNT(*) n, COUNT(DISTINCT origin) apps FROM sessions"
BUG_SEVERITY = "SELECT severity, COUNT(*) n FROM suspects WHERE status = 'confirmed' GROUP BY severity"
SUSPECT_COUNTS = "SELECT status, COUNT(*) n FROM suspects WHERE session_id = ? GROUP BY status"
PAGE_COUNTS = ("SELECT COUNT(*) total, COALESCE(SUM(visited), 0) visited FROM pages WHERE session_id = ?")
GET_SETTING = "SELECT value FROM settings WHERE key = ?"
SET_SETTING = ("INSERT INTO settings(key, value) VALUES(?, ?) "
               "ON CONFLICT(key) DO UPDATE SET value = excluded.value")


def norm_url(url: str) -> str:
    """Page identity for the map: scheme+host+path (numeric ids folded), query keys only, no fragment."""
    try:
        p = urlsplit((url or "").strip())
    except ValueError:
        return (url or "").strip()
    if not p.scheme:
        return (url or "").strip()
    path = re.sub(r"/\d+(?=/|$)", "/:id", p.path or "/")
    path = path.rstrip("/") or "/"
    query = "&".join(sorted({q.split("=")[0] for q in p.query.split("&") if q})) if p.query else ""
    return urlunsplit((p.scheme.lower(), p.netloc.lower(), path, query, ""))


def fingerprint(url: str, title: str) -> str:
    """Same page + same significant title words = same issue (used to drop duplicate reports)."""
    words = " ".join(sorted(set(re.findall(r"[a-z0-9]{3,}", (title or "").lower()))))
    base = f"{norm_url(url or '')}|{words}"
    return hashlib.sha256(base.encode()).hexdigest()[:20]


def _dump(col: str, v):
    return json.dumps(v) if col in JSON_COLS and v is not None and not isinstance(v, str) else v


def _insert_params(defaults: dict, fields: dict) -> dict:
    bad = set(fields) - set(defaults)
    if bad:
        raise ValueError(f"unknown columns: {sorted(bad)}")
    return {c: _dump(c, fields.get(c, d)) for c, d in defaults.items()}


def _update_params(defaults: dict, row_id: int, fields: dict) -> dict:
    bad = set(fields) - set(defaults)
    if bad:
        raise ValueError(f"unknown columns: {sorted(bad)}")
    params: dict = {"id": row_id}
    for c in defaults:
        params[c] = _dump(c, fields.get(c))
        params["u_" + c] = 1 if c in fields else 0
    return params


def _row(r: sqlite3.Row | None) -> dict | None:
    if r is None:
        return None
    d = dict(r)
    for k in JSON_COLS & d.keys():
        try:
            d[k] = json.loads(d[k]) if d[k] else ({} if k == "charter" else [])
        except (TypeError, ValueError):
            pass
    return d


def safe_db_path(path) -> Path:
    """Absolute, normalized database path; must be a .db file."""
    full = os.path.normpath(os.path.abspath(os.fspath(path)))
    if not full.endswith(".db") or os.path.isdir(full):
        raise ValueError(f"Database path must be a .db file: {full}")
    return Path(full)


class Store:
    def __init__(self, path):
        self.path = safe_db_path(path)
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self._local = threading.local()
        self.conn.executescript(SCHEMA)

    @property
    def conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = sqlite3.connect(self.path, timeout=30, isolation_level=None)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA busy_timeout=30000")
            self._local.conn = c
        return c

    @contextmanager
    def tx(self):
        c = self.conn
        c.execute("BEGIN IMMEDIATE")
        try:
            yield c
            c.execute("COMMIT")
        except BaseException:
            c.execute("ROLLBACK")
            raise

    # ------------------------------------------------------------ settings
    def get_setting(self, key: str, default: str | None = None) -> str | None:
        r = self.conn.execute(GET_SETTING, (key,)).fetchone()
        return r["value"] if r else default

    def set_setting(self, key: str, value: str) -> None:
        self.conn.execute(SET_SETTING, (key, value))

    # ------------------------------------------------------------ sessions
    def add_session(self, **fields) -> int:
        fields.setdefault("created_at", time.time())
        return int(self.conn.execute(INSERT_SESSION, _insert_params(SESSION_DEFAULTS, fields)).lastrowid)

    def get_session(self, sid: int) -> dict | None:
        return _row(self.conn.execute(SELECT_SESSION, (sid,)).fetchone())

    def update_session(self, sid: int, **fields) -> None:
        if fields:
            self.conn.execute(UPDATE_SESSION, _update_params(SESSION_DEFAULTS, sid, fields))

    def list_sessions(self, limit: int = 100, statuses: tuple | None = None) -> list[dict]:
        out = [_row(r) for r in self.conn.execute(LIST_SESSIONS, (limit,)).fetchall()]
        return [s for s in out if not statuses or s["status"] in statuses]

    def add_actions(self, sid: int, n: int = 1) -> int:
        self.conn.execute(ADD_ACTIONS, (n, sid))
        r = self.conn.execute(SELECT_ACTIONS, (sid,)).fetchone()
        return int(r["actions_used"]) if r else 0

    # ------------------------------------------------------------ tasks
    def add_task(self, session_id: int, kind: str, title: str, charter: dict | None = None,
                 suspect_id: int | None = None, action_budget: int | None = None) -> int:
        fields = {"session_id": session_id, "kind": kind, "priority": PRIORITY.get(kind, 0), "title": title,
                  "charter": charter or {}, "suspect_id": suspect_id, "action_budget": action_budget,
                  "created_at": time.time()}
        return int(self.conn.execute(INSERT_TASK, _insert_params(TASK_DEFAULTS, fields)).lastrowid)

    def get_task(self, tid: int) -> dict | None:
        return _row(self.conn.execute(SELECT_TASK, (tid,)).fetchone())

    def update_task(self, tid: int, **fields) -> None:
        if fields:
            self.conn.execute(UPDATE_TASK, _update_params(TASK_DEFAULTS, tid, fields))

    def tasks_for(self, sid: int) -> list[dict]:
        return [_row(r) for r in self.conn.execute(TASKS_FOR, (sid,)).fetchall()]

    def running_tasks(self) -> list[dict]:
        return [_row(r) for r in self.conn.execute(RUNNING_TASKS).fetchall()]

    def claim_next(self) -> dict | None:
        """Highest-priority queued task of a session that is still running."""
        with self.tx() as c:
            r = c.execute(NEXT_TASK).fetchone()
            if not r:
                return None
            c.execute(CLAIM_TASK, (time.time(), r["id"]))
        return self.get_task(r["id"])

    def recover(self) -> int:
        """Tasks interrupted by a restart go back to the queue."""
        with self.tx() as c:
            return c.execute(RECOVER_TASKS).rowcount

    def tasks_with_status(self, sid: int, status: str) -> list[dict]:
        return [_row(r) for r in self.conn.execute(TASKS_BY_STATUS, (sid, status)).fetchall()]

    def cancel_queued(self, sid: int, kind: str | None, reason: str) -> int:
        if kind is None:
            return self.conn.execute(CANCEL_QUEUED_ALL, (time.time(), reason, sid)).rowcount
        return self.conn.execute(CANCEL_QUEUED_KIND, (time.time(), reason, sid, kind)).rowcount

    def stats(self) -> dict:
        r = self.conn.execute(COUNT_SESSIONS).fetchone()
        sev = {k: 0 for k in SEVERITIES}
        for row in self.conn.execute(BUG_SEVERITY).fetchall():
            sev[row["severity"]] = row["n"]
        return {"sessions": r["n"], "apps": r["apps"], "bugs": sum(sev.values()), "by_severity": sev}

    def session_counts(self, sid: int) -> dict:
        out = {row["status"]: row["n"] for row in self.conn.execute(SUSPECT_COUNTS, (sid,)).fetchall()}
        p = self.conn.execute(PAGE_COUNTS, (sid,)).fetchone()
        out["pages_total"], out["pages_visited"] = p["total"], p["visited"]
        return out

    # ------------------------------------------------------------ map
    def upsert_page(self, sid: int, url: str, title: str | None = None, kind: str | None = None,
                    visited: bool = False, components: list | None = None) -> str:
        u = norm_url(url)
        with self.tx() as c:
            r = c.execute(SELECT_PAGE, (sid, u)).fetchone()
            if r is None:
                c.execute(INSERT_PAGE, (sid, u, title, kind, int(visited),
                                        json.dumps([str(x)[:120] for x in components or []][:60]), time.time()))
            else:
                comps = json.loads(r["components"] or "[]")
                for x in components or []:
                    if str(x)[:120] not in comps:
                        comps.append(str(x)[:120])
                c.execute(UPDATE_PAGE, (title, kind, int(visited), json.dumps(comps[:60]), r["id"]))
        return u

    def add_edge(self, sid: int, src: str, dst: str, label: str | None = None) -> None:
        s, d = norm_url(src), norm_url(dst)
        if not s or not d or s == d:
            return
        self.upsert_page(sid, s)
        self.upsert_page(sid, d)
        self.conn.execute(INSERT_EDGE, (sid, s, d, (label or "")[:80] or None))

    def pages(self, sid: int) -> list[dict]:
        return [_row(r) for r in self.conn.execute(LIST_PAGES, (sid,)).fetchall()]

    def edges(self, sid: int) -> list[dict]:
        return [dict(r) for r in self.conn.execute(LIST_EDGES, (sid,)).fetchall()]

    # ------------------------------------------------------------ suspects / bugs
    def add_suspect(self, sid: int, task_id: int | None, data: dict) -> int | None:
        """Returns the new suspect id, or None when the same issue was already reported."""
        now = time.time()
        sev = str(data.get("severity") or "medium").lower()
        try:
            conf = float(data["confidence"]) if data.get("confidence") is not None else None
            if conf is not None and conf > 1:
                conf = conf / 100.0
        except (TypeError, ValueError):
            conf = None
        steps = data.get("steps") or []
        if isinstance(steps, str):
            steps = [s for s in steps.splitlines() if s.strip()]
        evidence = data.get("evidence") or []
        if isinstance(evidence, str):
            evidence = [evidence]
        fields = {
            "session_id": sid, "task_id": task_id,
            "fingerprint": fingerprint(str(data.get("url") or ""), str(data.get("title") or "")),
            "title": str(data.get("title") or "Untitled issue")[:200],
            "description": str(data.get("description") or "")[:4000],
            "steps": [str(s)[:500] for s in steps][:40],
            "expected": str(data.get("expected") or "")[:2000],
            "actual": str(data.get("actual") or "")[:2000],
            "severity": sev if sev in SEVERITIES else "medium",
            "confidence": conf, "url": str(data.get("url") or "")[:1000],
            "evidence": [str(e)[:1000] for e in evidence][:30],
            "created_at": now, "updated_at": now}
        try:
            return int(self.conn.execute(INSERT_SUSPECT, _insert_params(SUSPECT_DEFAULTS, fields)).lastrowid)
        except sqlite3.IntegrityError:
            return None

    def get_suspect(self, sid: int) -> dict | None:
        return _row(self.conn.execute(SELECT_SUSPECT, (sid,)).fetchone())

    def update_suspect(self, sid: int, **fields) -> None:
        fields.setdefault("updated_at", time.time())
        self.conn.execute(UPDATE_SUSPECT, _update_params(SUSPECT_DEFAULTS, sid, fields))

    def suspects(self, session_id: int | None = None, status: str | None = None) -> list[dict]:
        rows = self.conn.execute(LIST_SUSPECTS, (session_id, session_id, status, status)).fetchall()
        return [_row(r) for r in rows]

    # ------------------------------------------------------------ events / usage
    def add_event(self, session_id: int | None, task_id: int | None, source: str, kind: str, text: str) -> None:
        self.conn.execute(INSERT_EVENT, (session_id, task_id, time.time(), source, kind, str(text)[:2000]))

    def events(self, session_id: int | None = None, limit: int = 150) -> list[dict]:
        return [dict(r) for r in self.conn.execute(LIST_EVENTS, (session_id, session_id, limit)).fetchall()]

    def add_usage(self, session_id: int, task_id: int, cost: float, tin: int, tout: int) -> None:
        self.conn.execute(INSERT_USAGE, (time.time(), session_id, task_id, cost, tin, tout))
        self.conn.execute(ADD_SESSION_COST, (cost, session_id))

    def usage_total(self) -> dict:
        r = self.conn.execute(USAGE_TOTAL).fetchone()
        return {"cost_usd": r["c"], "output_tokens": r["o"], "input_tokens": r["i"]}
