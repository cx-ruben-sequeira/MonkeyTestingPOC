"""Dashboard server: stdlib ThreadingHTTPServer on 127.0.0.1, JSON API plus media from runs/.

Every POST needs the X-MT: 1 header and, when the browser sends one, an Origin equal to the
dashboard's own; the Host header must be the dashboard's (no DNS rebinding). Media under /media/
is served only from inside runs/, with Range support so the video player can seek.
"""
from __future__ import annotations

import csv
import io
import json
import logging
import mimetypes
import os
import re
import threading
import time
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from . import jira, jira_settings, sessions
from .agents.reporter import build_markdown
from .config import ROOT, Config
from .store import SESSION_ACTIVE, Store

log = logging.getLogger("mt.server")
WEB = ROOT / "web" / "index.html"
MEDIA_TYPES = {".webm": "video/webm", ".mp4": "video/mp4", ".png": "image/png", ".jpg": "image/jpeg",
               ".jpeg": "image/jpeg", ".ts": "text/plain; charset=utf-8", ".md": "text/markdown; charset=utf-8",
               ".zip": "application/zip"}
IMAGE_EXT = (".png", ".jpg", ".jpeg")


class App:
    def __init__(self, cfg: Config, store: Store):
        self.cfg, self.store = cfg, store
        self.jira_lock = threading.Lock()

    # ------------------------------------------------------------ helpers
    def media_url(self, path: str | None) -> str | None:
        if not path:
            return None
        base = os.path.normpath(os.path.abspath(os.fspath(self.cfg.runs_dir)))
        full = os.path.normpath(os.path.abspath(path))
        if not full.startswith(base + os.sep):
            return None
        return "/media/" + os.path.relpath(full, base).replace(os.sep, "/")

    def latest_shot(self, task: dict) -> str | None:
        base = os.path.normpath(os.path.abspath(os.fspath(self.cfg.runs_dir)))
        tdir = os.path.normpath(os.path.join(base, f"s{int(task['session_id'])}", f"t{int(task['id'])}"))
        if not tdir.startswith(base + os.sep) or not os.path.isdir(tdir):
            return None
        best, best_t = None, 0.0
        for sub in ("browser", "shots"):
            for root, _dirs, files in os.walk(os.path.join(tdir, sub)):
                for f in files:
                    if f.lower().endswith(IMAGE_EXT) and ".tmp." not in f:
                        p = os.path.join(root, f)
                        t = os.path.getmtime(p)
                        if t > best_t:
                            best, best_t = p, t
        return self.media_url(best)

    def action_shots(self, sid: int) -> list[dict]:
        """Every per-action screenshot of a session (runs/s<sid>/t<tid>/shots/*.png), oldest first."""
        base = os.path.normpath(os.path.abspath(os.fspath(self.cfg.runs_dir)))
        sdir = os.path.normpath(os.path.join(base, f"s{int(sid)}"))
        if not sdir.startswith(base + os.sep) or not os.path.isdir(sdir):
            return []
        kinds = {int(t["id"]): t["kind"] for t in self.store.tasks_for(sid)}
        out = []
        for tname in os.listdir(sdir):
            m = re.match(r"^t(\d+)$", tname)
            shots = os.path.join(sdir, tname, "shots")
            if not m or not os.path.isdir(shots):
                continue
            tid = int(m.group(1))
            for f in os.listdir(shots):
                fm = re.match(r"^(\d{19})-([a-z_]{0,30})\.png$", f)
                if not fm:
                    continue
                info = {}
                meta = os.path.join(shots, f[:-4] + ".json")
                if os.path.isfile(meta):
                    try:
                        with open(meta, "r", encoding="utf-8") as fh:
                            info = json.load(fh)
                    except ValueError:
                        info = {}
                out.append({"ns": int(fm.group(1)), "task": tid, "kind": kinds.get(tid, "task"),
                            "action": fm.group(2), "target": str(info.get("target") or ""),
                            "path": os.path.join(shots, f)})
        return sorted(out, key=lambda x: x["ns"])

    def shots_zip(self, sid: int) -> bytes:
        """All per-action screenshots of a closed session as one zip, numbered in the order they were taken."""
        s = self.store.get_session(sid)
        if not s:
            raise KeyError("no such session")
        if s["status"] in SESSION_ACTIVE:
            raise sessions.FormError("the session is still running: the zip is available once it ends")
        shots = self.action_shots(sid)
        if not shots:
            raise KeyError("this session has no screenshots")
        buf, index = io.BytesIO(), io.StringIO()
        rows = csv.writer(index)
        rows.writerow(["#", "time", "task", "role", "action", "target", "file"])
        folder = f"session-{int(sid)}-screenshots"
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:  # PNGs are already compressed
            for i, x in enumerate(shots, 1):
                name = f"{i:04d}_t{x['task']}-{x['kind']}_{x['action'] or 'action'}.png"
                when = time.localtime(x["ns"] / 1e9)
                z.write(x["path"], f"{folder}/{name}")
                rows.writerow([i, time.strftime("%Y-%m-%d %H:%M:%S", when), x["task"], x["kind"], x["action"],
                               x["target"], name])
            z.writestr(f"{folder}/index.csv", index.getvalue())
        return buf.getvalue()

    def suspect_view(self, s: dict) -> dict:
        out = dict(s)
        out["video_url"] = self.media_url(s.get("video_path"))
        out["screenshot_urls"] = [u for u in (self.media_url(p) for p in s.get("screenshots") or []) if u]
        out["spec_url"] = self.media_url(s.get("spec_path"))
        return out

    # ------------------------------------------------------------ GET
    def state(self, sid: int | None) -> dict:
        st = self.store
        all_sessions = st.list_sessions(100)
        rows = []
        for s in all_sessions[:50]:
            c = st.session_counts(int(s["id"]))
            rows.append({k: s[k] for k in ("id", "name", "url", "mode", "status", "phase", "duration_min",
                                           "max_actions", "actions_used", "repeat_count", "created_at",
                                           "started_at", "deadline", "finished_at", "cost_usd", "summary", "delete_state")}
                        | {"bugs": c.get("confirmed", 0), "suspects": sum(v for k, v in c.items()
                                                                          if not k.startswith("pages")),
                           "pages_total": c.get("pages_total", 0), "pages_visited": c.get("pages_visited", 0)})
        workers = []
        for t in st.running_tasks():
            s = st.get_session(int(t["session_id"]))
            workers.append({k: t[k] for k in ("id", "session_id", "kind", "title", "worker_slot", "step_name",
                                              "current_action", "actions", "action_budget", "tokens_out",
                                              "started_at", "suspect_id")}
                           | {"session_url": s["url"] if s else "", "screenshot": self.latest_shot(t)})
        queued = sum(1 for s in all_sessions if s["status"] in ("running", "reporting")
                     for t in st.tasks_with_status(int(s["id"]), "queued"))
        out = {"now": time.time(), "max_workers": int(st.get_setting("max_workers", str(self.cfg.max_workers))),
               "jira_ready": self.cfg.jira_ready, "jira_read_ready": self.cfg.jira_read_ready, "stats": st.stats(), "usage": st.usage_total(),
               "sessions": rows, "workers": workers, "queued": queued}
        if sid:
            out["selected"] = self.session_detail(sid)
        return out

    def session_detail(self, sid: int) -> dict | None:
        st = self.store
        s = st.get_session(sid)
        if not s:
            return None
        return {"session": s, "counts": st.session_counts(sid),
                "tasks": [{k: t[k] for k in ("id", "kind", "status", "title", "actions", "action_budget",
                                             "tokens_out", "cost_usd", "result", "error", "stop_reason",
                                             "started_at", "finished_at", "suspect_id", "worker_slot",
                                             "current_action")} for t in st.tasks_for(sid)],
                "suspects": [self.suspect_view(x) for x in st.suspects(sid)],
                "events": st.events(sid, 200), "pages": st.pages(sid), "edges": st.edges(sid),
                "has_report": bool(s.get("report_path")),
                "shot_count": len(self.action_shots(sid)) if s.get("shots_every_action") else 0}

    def bugs(self) -> list[dict]:
        out = []
        cache: dict = {}
        for b in self.store.suspects(None, "confirmed"):
            if b["duplicate_of"]:
                continue
            s = cache.setdefault(b["session_id"], self.store.get_session(b["session_id"]))
            out.append(self.suspect_view(b) | {"session_url": s["url"] if s else "", "session_name": s["name"] if s else ""})
        return out

    def report_md(self, sid: int) -> str | None:
        s = self.store.get_session(sid)
        if not s:
            return None
        path = s.get("report_path")
        if path:
            base = os.path.normpath(os.path.abspath(os.fspath(self.cfg.runs_dir)))
            full = os.path.normpath(os.path.abspath(path))
            if full.startswith(base + os.sep) and os.path.isfile(full):
                with open(full, "r", encoding="utf-8") as fh:
                    return fh.read()
        return build_markdown(self.store, sid, "")  # live draft while the session runs

    # ------------------------------------------------------------ POST
    def set_workers(self, body: dict) -> dict:
        n = int(body.get("max_workers") or 0)
        if not 1 <= n <= 10:
            raise sessions.FormError("max_workers must be 1-10")
        self.store.set_setting("max_workers", str(n))
        return {"max_workers": n}

    def jira_test(self) -> dict:
        if not self.cfg.jira_read_ready:
            raise sessions.FormError("fill in the Jira URL, email and API token first")
        me = jira.myself(self.cfg)
        return {"ok": True, "name": me["name"], "email": me["email"]}

    def delete_session(self, sid: int) -> dict:
        from . import deletion
        return deletion.request(self.cfg, self.store, sid)

    def delete_many(self, ids) -> dict:
        from . import deletion
        if not isinstance(ids, list) or not ids or len(ids) > 500 or not all(isinstance(i, int) and i > 0 for i in ids):
            raise sessions.FormError("ids must be a list of session numbers")
        results = []
        for sid in dict.fromkeys(ids):
            try:
                results.append(deletion.request(self.cfg, self.store, sid))
            except KeyError:
                results.append({"id": sid, "state": "missing", "deleted": False})
        return {"results": results, "deleted": sum(1 for r in results if r.get("deleted"))}

    def delete_preview(self, sid: int) -> dict:
        from . import deletion
        out = deletion.preview(self.cfg, self.store, sid)
        if out is None:
            raise KeyError("no such session")
        return out

    def cancel(self, sid: int) -> dict:
        s = self.store.get_session(sid)
        if not s:
            raise KeyError("no such session")
        if s["status"] in ("queued", "running", "reporting"):
            self.store.update_session(sid, cancel_requested=1)
            self.store.add_event(sid, None, "dashboard", "info", "cancel requested")
        return {"ok": True}

    def open_in_jira(self, bug_id: int) -> dict:
        with self.jira_lock:  # one ticket per bug, even with a double click
            b = self.store.get_suspect(bug_id)
            if not b or b["status"] != "confirmed":
                raise KeyError("no such confirmed bug")
            if b["jira_key"]:
                return {"key": b["jira_key"], "url": b["jira_url"], "existing": True}
            s = self.store.get_session(b["session_id"])
            issue = jira.create_issue(self.cfg, b, s)
            self.store.update_suspect(bug_id, jira_key=issue["key"], jira_url=issue["url"])
            files = [p for p in [b.get("video_path")] + list(b.get("screenshots") or []) if p]
            attached, warn = [], None
            try:
                attached = jira.attach(self.cfg, issue["key"], files, str(self.cfg.runs_dir))
            except jira.JiraError as e:
                warn = f"ticket created, but attaching files failed: {e}"
            self.store.add_event(b["session_id"], None, "jira", "info",
                                 f"bug #{bug_id} -> {issue['key']} ({len(attached)} attachments)")
            return {**issue, "attached": attached, "warning": warn}


class Handler(BaseHTTPRequestHandler):
    app: App
    server_version = "MonkeyTesting"

    def log_message(self, fmt, *args):  # quiet
        log.debug("%s %s", self.address_string(), fmt % args)

    # ------------------------------------------------------------ plumbing
    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").lower()
        port = self.app.cfg.port
        return host in (f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}")

    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj, default=str).encode(), "application/json")

    def _err(self, code: int, msg: str) -> None:
        self._json({"error": msg}, code)

    # ------------------------------------------------------------ GET
    def do_GET(self):
        if not self._host_ok():
            return self._err(403, "bad host")
        u = urlsplit(self.path)
        q = parse_qs(u.query)
        path = u.path
        try:
            if path in ("/", "/index.html"):
                with open(os.path.normpath(os.path.abspath(WEB)), "rb") as fh:
                    body = fh.read()
                return self._send(200, body, "text/html; charset=utf-8",
                                  {"Content-Security-Policy": "default-src 'self'; style-src 'self' 'unsafe-inline'; "
                                                              "script-src 'self' 'unsafe-inline'; img-src 'self' data:; "
                                                              "media-src 'self'; frame-ancestors 'none'"})
            if path == "/api/state":
                sid = int(q["session"][0]) if q.get("session") and q["session"][0].isdigit() else None
                return self._json(self.app.state(sid))
            if path == "/api/bugs":
                return self._json(self.app.bugs())
            if path == "/api/jira/settings":
                return self._json(jira_settings.view(self.app.cfg))
            m = re.match(r"^/api/report/(\d+)\.md$", path)
            if m:
                md = self.app.report_md(int(m.group(1)))
                if md is None:
                    return self._err(404, "no such session")
                return self._send(200, md.encode(), "text/markdown; charset=utf-8",
                                  {"Content-Disposition": f"attachment; filename=monkeytesting-session-{m.group(1)}.md"}
                                  if q.get("download") else None)
            m = re.match(r"^/api/sessions/(\d+)/delete-preview$", path)
            if m:
                return self._json(self.app.delete_preview(int(m.group(1))))
            m = re.match(r"^/api/sessions/(\d+)/screenshots\.zip$", path)
            if m:
                sid = int(m.group(1))
                return self._send(200, self.app.shots_zip(sid), "application/zip",
                                  {"Content-Disposition": f"attachment; filename=monkeytesting-session-{sid}-screenshots.zip"})
            if path.startswith("/media/"):
                return self._media(unquote(path[len("/media/"):]))
            return self._err(404, "not found")
        except sessions.FormError as e:
            return self._err(409, str(e))
        except KeyError as e:
            return self._err(404, str(e).strip("'"))
        except Exception as e:
            log.exception("GET %s", path)
            return self._err(500, str(e))

    do_HEAD = do_GET

    def _media(self, rel: str) -> None:
        base = os.path.normpath(os.path.abspath(os.fspath(self.app.cfg.runs_dir)))
        full = os.path.normpath(os.path.abspath(os.path.join(base, rel)))
        ext = os.path.splitext(full)[1].lower()
        if not full.startswith(base + os.sep) or ext not in MEDIA_TYPES or not os.path.isfile(full) \
                or os.path.basename(full) == "secrets.env":
            return self._err(404, "not found")
        size = os.path.getsize(full)
        start, end, code = 0, size - 1, 200
        rng = self.headers.get("Range") or ""
        m = re.match(r"^bytes=(\d*)-(\d*)$", rng)
        if m and size:
            if m.group(1):
                start = int(m.group(1))
                end = min(int(m.group(2)) if m.group(2) else size - 1, size - 1)
            elif m.group(2):
                start = max(0, size - int(m.group(2)))
            if start > end:
                return self._send(416, b"", "text/plain", {"Content-Range": f"bytes */{size}"})
            code = 206
        with open(full, "rb") as fh:
            fh.seek(start)
            body = fh.read(end - start + 1)
        extra = {"Accept-Ranges": "bytes"}
        if code == 206:
            extra["Content-Range"] = f"bytes {start}-{end}/{size}"
        self._send(code, body, MEDIA_TYPES[ext], extra)

    # ------------------------------------------------------------ POST
    def _write_allowed(self) -> bool:
        """Every write (POST, DELETE) needs the dashboard's Host, the X-MT header and, when sent, its Origin."""
        if not self._host_ok():
            self._err(403, "bad host")
            return False
        origin = self.headers.get("Origin")
        port = self.app.cfg.port
        if self.headers.get("X-MT") != "1" or (origin and origin not in (
                f"http://127.0.0.1:{port}", f"http://localhost:{port}", f"http://[::1]:{port}")):
            self._err(403, "forbidden")
            return False
        return True

    def do_DELETE(self):
        if not self._write_allowed():
            return
        path = urlsplit(self.path).path
        m = re.match(r"^/api/sessions/(\d+)$", path)
        if not m:
            return self._err(404, "not found")
        try:
            return self._json(self.app.delete_session(int(m.group(1))))
        except KeyError as e:
            return self._err(404, str(e).strip("'"))
        except ValueError as e:
            return self._err(400, str(e))
        except Exception as e:
            log.exception("DELETE %s", path)
            return self._err(500, str(e))

    def do_POST(self):
        if not self._write_allowed():
            return
        length = int(self.headers.get("Content-Length") or 0)
        if length > 1_000_000:
            return self._err(413, "too large")
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(body, dict):
                raise ValueError
        except ValueError:
            return self._err(400, "body must be a JSON object")
        path = urlsplit(self.path).path
        app = self.app
        try:
            if path == "/api/sessions":
                return self._json(sessions.create(app.cfg, app.store, body))
            if path == "/api/settings":
                return self._json(app.set_workers(body))
            if path == "/api/jira/settings":
                return self._json(jira_settings.save(app.cfg, app.store, body))
            if path == "/api/jira/test":
                return self._json(app.jira_test())
            m = re.match(r"^/api/sessions/(\d+)/cancel$", path)
            if m:
                return self._json(app.cancel(int(m.group(1))))
            if path == "/api/sessions/delete":   # bulk delete: {"ids": [1, 2, 3]}
                return self._json(app.delete_many(body.get("ids")))
            m = re.match(r"^/api/bugs/(\d+)/jira$", path)
            if m:
                return self._json(app.open_in_jira(int(m.group(1))))
            return self._err(404, "not found")
        except (sessions.FormError, jira.JiraError, jira_settings.SettingsError) as e:
            return self._err(400, str(e))
        except KeyError as e:
            return self._err(404, str(e).strip("'"))
        except Exception as e:
            log.exception("POST %s", path)
            return self._err(500, str(e))


def make_server(cfg: Config, store: Store, port: int | None = None) -> ThreadingHTTPServer:
    jira_settings.apply(cfg, store)   # the Jira connection saved from the dashboard
    app = App(cfg, store)
    handler = type("BoundHandler", (Handler,), {"app": app})
    srv = ThreadingHTTPServer((cfg.host, cfg.port if port is None else port), handler)
    srv.daemon_threads = True
    return srv
