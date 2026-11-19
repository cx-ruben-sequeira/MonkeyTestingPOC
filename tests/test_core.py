"""Unit tests: fake Claude (no tokens), stub Jira (no real calls). Run:
    python3 -m unittest discover -s tests -v
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import warnings
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "hooks"))

import guard  # noqa: E402
from mtlib import config, repro, sessions  # noqa: E402
from mtlib.dispatcher import Dispatcher  # noqa: E402
from mtlib.server import make_server  # noqa: E402
from mtlib.store import Store, fingerprint, norm_url  # noqa: E402
from mtlib.worker import Masker, StreamParser, checked_argv  # noqa: E402

FAKE = ROOT / "tests" / "fake_claude.py"
DEMO = "http://localhost:8099"


def read_under(base, path) -> str:
    """Reads a test artifact after checking it lies inside the test's temp folder."""
    b = os.path.normpath(os.path.abspath(os.fspath(base)))
    p = os.path.normpath(os.path.abspath(os.fspath(path)))
    if not p.startswith(b + os.sep) or not os.path.isfile(p):
        raise AssertionError(f"{p} is not a file under {b}")
    with open(p, "r", encoding="utf-8") as fh:
        return fh.read()


def write_under(base, path, data) -> None:
    """Writes a test fixture after checking it lies inside the test's temp folder."""
    b = os.path.normpath(os.path.abspath(os.fspath(base)))
    p = os.path.normpath(os.path.abspath(os.fspath(path)))
    if not p.startswith(b + os.sep):
        raise AssertionError(f"{p} is not under {b}")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    mode = "wb" if isinstance(data, bytes) else "w"
    with open(p, mode) as fh:
        fh.write(data)


def fetch(req, timeout: float = 10) -> tuple[int, bytes]:
    """urlopen that checks the status code; HTTP errors come back as (code, body)."""
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.status
            if not 200 <= status < 300:
                raise AssertionError(f"unexpected HTTP status {status}")
            return status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def make_cfg(tmp: Path):
    cfg = config.load()
    cfg.data_dir = tmp
    cfg.claude_bin = f"{sys.executable} {FAKE}"
    cfg.model = cfg.effort = ""
    cfg.max_budget_usd = 0
    cfg.max_attempts = 2
    cfg.max_rounds = 0
    cfg.jira_base_url = cfg.jira_email = cfg.jira_project_key = ""
    cfg.ensure_dirs()
    return cfg


async def fake_repro(cfg, work_dir, n, env_extra=None, on_progress=None, timeout_s=None):
    """Confirms every suspect except ones titled '... sometimes ...', which fail in only some runs."""
    store = Store(cfg.db_path)
    sid = int(Path(work_dir).name.split("-")[1])
    title = store.get_suspect(sid)["title"]
    failed = n // 2 if "sometimes" in title else n
    for i in range(1, n + 1):
        if on_progress:
            on_progress(i, min(i, failed), n)
    results = [{"status": "failed" if i < failed else "passed", "message": "Error: expect(locator).toBeVisible()",
                "duration_ms": 10, "attachments": []} for i in range(n)]
    out = repro.decide(results, n)
    out["runs"] = results
    return out


class Base(unittest.TestCase):
    def setUp(self):
        warnings.simplefilter("ignore", ResourceWarning)  # per-thread sqlite connections
        self.tmp = Path(tempfile.mkdtemp(prefix="mt-test-")).resolve()
        self.cfg = make_cfg(self.tmp)
        self.store = Store(self.cfg.db_path)
        os.environ["FAKE_CLAUDE_STATE"] = str(self.tmp)
        os.environ.pop("FAKE_CLAUDE_MODE", None)

    def tearDown(self):
        os.environ.pop("FAKE_CLAUDE_MODE", None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def new_session(self, **over):
        form = {"url": DEMO + "/", "mode": "hybrid", "duration_min": 5, "max_actions": 200, "repeat_count": 3}
        form.update(over)
        return sessions.create(self.cfg, self.store, form)["id"]

    def run_all(self, timeout=60):
        disp = Dispatcher(self.cfg, self.store, repro_fn=fake_repro, tick_s=0.05)

        async def go():
            await disp.drain(timeout)
        asyncio.run(go())
        return disp


# ---------------------------------------------------------------- pure helpers
class TestHelpers(unittest.TestCase):
    def test_norm_url_folds_ids_and_query_values(self):
        self.assertEqual(norm_url("http://A.test/orders/123/?b=2&a=1#x"), "http://a.test/orders/:id?a&b")
        self.assertEqual(fingerprint("http://a.test/x", "Save button crashes"),
                         fingerprint("http://a.test/x/", "crashes: save BUTTON"))

    def test_checked_argv_refuses_free_text(self):
        self.assertEqual(checked_argv(["claude", "-p", "--tools", "Read,Write"]), ["claude", "-p", "--tools", "Read,Write"])
        for bad in (["claude", "hello world"], ["claude", "--x=$(id)"], ["claude", "a;b"]):
            with self.assertRaises(ValueError):
                checked_argv(bad)

    def test_parser_markers_and_masking(self):
        p = StreamParser(Masker(["s3cret!"]))
        evs = p.feed({"type": "assistant", "message": {"id": "m1", "usage": {"output_tokens": 5}, "content": [
            {"type": "text", "text": 'hi\nSUSPECT: {"title": "x", "actual": "pw s3cret! leaked"}\n`PAGE: {"url": "http://a/"}`'}]}})
        names = [e.name for e in evs if e.kind == "marker"]
        self.assertEqual(names, ["SUSPECT", "PAGE"])
        self.assertNotIn("s3cret!", evs[0].data["actual"])
        evs = p.feed({"type": "assistant", "message": {"id": "m2", "content": [
            {"type": "tool_use", "id": "t1", "name": "mcp__playwright__browser_click", "input": {"element": "Save"}}]}})
        self.assertTrue(evs[0].browser)
        evs = p.feed({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1",
                                                               "content": "MonkeyTesting guard blocked this action"}]}})
        self.assertEqual(evs[0].kind, "blocked")

    def test_guard(self):
        rules = {"origins": ["http://localhost:8099"], "allow_destructive": False}
        self.assertIsNotNone(guard.decide("mcp__playwright__browser_click", {"element": "Delete all data button"}, rules))
        self.assertIsNone(guard.decide("mcp__playwright__browser_click", {"element": "Search button"}, rules))
        self.assertIsNotNone(guard.decide("mcp__playwright__browser_navigate", {"url": "https://evil.example/"}, rules))
        self.assertIsNone(guard.decide("mcp__playwright__browser_navigate", {"url": "http://localhost:8099/a"}, rules))
        self.assertIsNone(guard.decide("mcp__playwright__browser_click", {"element": "Delete all"},
                                       dict(rules, allow_destructive=True)))

    def test_guard_hook_process(self):
        with tempfile.TemporaryDirectory() as raw:
            d = os.path.realpath(raw)
            gf = os.path.join(d, "guard.json")
            write_under(d, gf, json.dumps({"origins": ["http://localhost:8099"]}))
            env = dict(os.environ, MT_GUARD_FILE=gf, MT_BLOCK_LOG=os.path.join(d, "blocked.log"))
            payload = {"tool_name": "mcp__playwright__browser_click", "tool_input": {"element": "Pay now"}}
            out = subprocess.run([sys.executable, str(ROOT / "hooks" / "guard.py")], input=json.dumps(payload),
                                 capture_output=True, text=True, env=env, check=True).stdout
            self.assertEqual(json.loads(out)["hookSpecificOutput"]["permissionDecision"], "deny")
            self.assertIn("BLOCKED", read_under(d, os.path.join(d, "blocked.log")))
            # soft stop: once the orchestrator writes the flag, every browser action is refused with a wrap-up note
            flag = os.path.join(d, "stop.flag")
            write_under(d, flag, "this worker's action budget is used up.")
            safe = {"tool_name": "mcp__playwright__browser_snapshot", "tool_input": {}}
            out = subprocess.run([sys.executable, str(ROOT / "hooks" / "guard.py")], input=json.dumps(safe),
                                 capture_output=True, text=True, env=dict(env, MT_STOP_FILE=flag), check=True).stdout
            reason = json.loads(out)["hookSpecificOutput"]["permissionDecisionReason"]
            self.assertIn("MonkeyTesting limit", reason)
            self.assertIn("marker", reason)

    def test_n_of_n_rule(self):
        f = lambda m="Error: expect(page).toHaveURL(/ok/) failed": {"status": "failed", "message": m}
        p = {"status": "passed", "message": ""}
        self.assertEqual(repro.decide([f()] * 5, 5)["verdict"], "confirmed")
        self.assertEqual(repro.decide([f(), f(), f(), p, p], 5)["verdict"], "not_confirmed")
        self.assertEqual(repro.decide([f(), f(), f("Error: expect(x).toBe(1) other")], 3)["verdict"], "not_confirmed")
        self.assertEqual(repro.decide([f("TimeoutError: locator.click")] * 3, 3)["verdict"], "not_confirmed")
        self.assertEqual(repro.decide([f()] * 2, 3)["verdict"], "error")
        # numbers inside the message do not make two failures different
        self.assertEqual(repro.decide([f("expect(x).toBe(1) got 3ms"), f("expect(x).toBe(1) got 9ms")], 2)["verdict"],
                         "confirmed")


# ---------------------------------------------------------------- sessions form
class TestSessions(Base):
    def test_validation_and_files(self):
        with self.assertRaises(sessions.FormError):
            sessions.create(self.cfg, self.store, {"url": "ftp://x"})
        with self.assertRaises(sessions.FormError):
            sessions.create(self.cfg, self.store, {"url": DEMO, "repeat_count": 50})
        with self.assertRaises(sessions.FormError) as e:
            sessions.create(self.cfg, self.store, {"url": "https://www.example.com"})
        self.assertIn("doesn't look like", str(e.exception))
        out = sessions.create(self.cfg, self.store, {"url": "https://www.example.com", "confirm_non_test": True})
        self.assertTrue(out["warning"])
        sid = self.new_session(username="bob", password="hunter22", repeat_count="")
        s = self.store.get_session(sid)
        self.assertEqual(s["repeat_count"], 5)  # default N
        self.assertEqual(s["has_auth"], 1)
        sec = self.cfg.runs_dir / f"s{sid}" / "secrets.env"
        self.assertEqual(stat.S_IMODE(sec.stat().st_mode), 0o600)
        self.assertNotIn("hunter22", json.dumps(s))
        self.assertTrue(sessions.looks_like_test_host("https://qa.shop.io"))
        self.assertTrue(sessions.looks_like_test_host("https://shop-staging.example.com"))
        self.assertFalse(sessions.looks_like_test_host("https://shop.example.com"))


# ---------------------------------------------------------------- orchestrator with fake Claude
class TestDispatcher(Base):
    def test_full_session(self):
        sid = self.new_session()
        self.run_all()
        s = self.store.get_session(sid)
        self.assertEqual(s["status"], "done")
        kinds = [t["kind"] for t in self.store.tasks_for(sid)]
        self.assertEqual(kinds.count("explore"), 1)
        self.assertEqual(kinds.count("test"), 2)
        self.assertEqual(kinds.count("report"), 1)
        sus = self.store.suspects(sid)
        self.assertEqual(len(sus), 3, [x["title"] for x in sus])  # the duplicate SUSPECT was dropped
        self.assertEqual(kinds.count("repro"), 3)
        by = {x["title"]: x for x in sus}
        self.assertEqual(by["Signup accepts an empty name"]["status"], "confirmed")
        self.assertEqual(by["Generate report returns 500"]["status"], "confirmed")
        flaky = by["Search for apple sometimes shows no results"]
        self.assertEqual((flaky["status"], flaky["runs_failed"], flaky["runs_total"]), ("not_confirmed", 1, 3))
        pages = {p["url"] for p in self.store.pages(sid)}
        self.assertIn(DEMO + "/signup", pages)
        self.assertIn(DEMO + "/pricing", pages)  # discovered, not visited
        self.assertFalse(next(p for p in self.store.pages(sid) if p["url"] == DEMO + "/pricing")["visited"])
        self.assertGreater(s["actions_used"], 5)
        events = self.store.events(sid, 500)
        self.assertTrue(any(e["kind"] == "blocked" for e in events))
        report = read_under(self.tmp, s["report_path"])
        self.assertIn("Signup accepts an empty name", report)
        self.assertIn("## Summary", report)
        self.assertIn("Not confirmed", report)
        self.assertGreater(s["cost_usd"], 0)
        self.assertTrue((self.cfg.runs_dir / f"s{sid}" / f"suspect-{sus[0]['id']}" / "playwright.config.ts").exists())

    def test_crash_is_retried(self):
        os.environ["FAKE_CLAUDE_MODE"] = "crash_once"
        sid = self.new_session()
        self.run_all()
        ex = next(t for t in self.store.tasks_for(sid) if t["kind"] == "explore")
        self.assertEqual((ex["status"], ex["attempts"]), ("done", 2))

    def test_crash_gives_up_and_falls_back(self):
        os.environ["FAKE_CLAUDE_MODE"] = "crash"
        sid = self.new_session()
        self.run_all()
        tasks = self.store.tasks_for(sid)
        self.assertEqual(next(t for t in tasks if t["kind"] == "explore")["status"], "failed")
        self.assertTrue(any(t["kind"] == "test" and t["title"] == "Whole application" for t in tasks))
        s = self.store.get_session(sid)
        self.assertEqual(s["status"], "done")
        self.assertIn("MonkeyTesting report", read_under(self.tmp, s["report_path"]))  # fact-only report

    def test_loop_guard(self):
        os.environ["FAKE_CLAUDE_MODE"] = "loop"
        sid = self.new_session()
        self.run_all()
        testers = [t for t in self.store.tasks_for(sid) if t["kind"] == "test"]
        self.assertTrue(testers and all(t["stop_reason"] == "loop" for t in testers))
        self.assertTrue(all(t["actions"] <= self.cfg.loop_repeat_limit + 2 for t in testers))

    def test_session_action_budget(self):
        os.environ["FAKE_CLAUDE_MODE"] = "budget"
        sid = self.new_session(max_actions=30)
        self.run_all()
        s = self.store.get_session(sid)
        self.assertEqual(s["status"], "done")
        self.assertTrue(s["budget_hit_at"])
        self.assertLess(s["actions_used"], 30 + 10)

    def test_cancel(self):
        os.environ["FAKE_CLAUDE_MODE"] = "slow"
        sid = self.new_session()
        disp = Dispatcher(self.cfg, self.store, repro_fn=fake_repro, tick_s=0.05)

        async def go():
            end = time.time() + 30
            while time.time() < end:
                disp.tick()
                if any(t["kind"] == "test" and t["status"] == "running" for t in self.store.tasks_for(sid)):
                    break
                await asyncio.sleep(0.05)
            self.store.update_session(sid, cancel_requested=1)
            await disp.drain(30)
        asyncio.run(go())
        self.assertEqual(self.store.get_session(sid)["status"], "cancelled")
        self.assertFalse([t for t in self.store.tasks_for(sid) if t["status"] in ("queued", "running")])

    def test_priority_and_recover(self):
        sid = self.new_session()
        self.store.update_session(sid, status="running")
        self.store.add_task(sid, "test", "t")
        self.store.add_task(sid, "explore", "e")
        self.store.add_task(sid, "repro", "r")
        order = [self.store.claim_next()["kind"] for _ in range(3)]
        self.assertEqual(order, ["repro", "explore", "test"])
        self.assertEqual(self.store.recover(), 3)
        self.assertEqual(self.store.claim_next()["kind"], "repro")


# ---------------------------------------------------------------- dashboard server + Jira stub
class JiraStub(BaseHTTPRequestHandler):
    calls: list = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n)
        JiraStub.calls.append((self.path, self.headers.get("Authorization"), body[:200]))
        if self.path == "/rest/api/3/issue":
            data = json.loads(body)
            assert data["fields"]["project"]["key"] == "QA"
            out = json.dumps({"key": "QA-7"}).encode()
            self.send_response(201)
        else:
            out = b"[]"
            self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


class TestServer(Base):
    def setUp(self):
        super().setUp()
        self.srv = make_server(self.cfg, self.store, port=0)
        self.cfg.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.cfg.port}"

    def tearDown(self):
        self.srv.shutdown()
        super().tearDown()

    def req(self, path, body=None, headers=None):
        h = {"X-MT": "1", "Content-Type": "application/json"}
        h.update(headers or {})
        r = urllib.request.Request(self.base + path, data=json.dumps(body).encode() if body is not None else None,
                                   headers=h, method="POST" if body is not None else "GET")
        code, raw = fetch(r)
        return code, json.loads(raw or b"null")

    def test_api_flow(self):
        code, out = self.req("/api/sessions", {"url": DEMO, "mode": "ai", "duration_min": 3, "repeat_count": 4})
        self.assertEqual(code, 200, out)
        sid = out["id"]
        code, st = self.req(f"/api/state?session={sid}")
        self.assertEqual(code, 200)
        self.assertEqual(st["selected"]["session"]["repeat_count"], 4)
        self.assertEqual(self.req("/api/sessions", {"url": DEMO}, {"X-MT": "0"})[0], 403)
        self.assertEqual(self.req("/api/settings", {"max_workers": 4}, {"Origin": "http://evil.test"})[0], 403)
        self.assertEqual(self.req("/api/settings", {"max_workers": 4})[1], {"max_workers": 4})
        self.assertEqual(self.req("/api/settings", {"max_workers": 40})[0], 400)
        self.assertEqual(self.req("/media/../data/mt.db")[0], 404)
        self.assertEqual(self.req(f"/media/s{sid}/guard.json")[0], 404)  # json isn't a media type
        self.assertEqual(self.req(f"/api/sessions/{sid}/cancel", {})[0], 200)
        code, page = fetch(urllib.request.Request(self.base + "/"))
        self.assertEqual(code, 200)
        self.assertIn(b"MonkeyTesting", page)

    def test_host_header_checked(self):
        code, _ = fetch(urllib.request.Request(self.base + "/api/state", headers={"Host": "evil.test"}))
        self.assertEqual(code, 403)

    def test_jira_create_once_and_attach(self):
        stub = ThreadingHTTPServer(("127.0.0.1", 0), JiraStub)
        threading.Thread(target=stub.serve_forever, daemon=True).start()
        JiraStub.calls = []
        try:
            self.cfg.jira_base_url = f"http://127.0.0.1:{stub.server_address[1]}"
            self.cfg.jira_email, self.cfg.jira_project_key = "qa@example.test", "QA"
            os.environ["JIRA_API_TOKEN"] = "tok"
            sid = self.new_session()
            bug = self.store.add_suspect(sid, None, {"title": "Broken save", "url": DEMO + "/x", "steps": ["a", "b"],
                                                     "expected": "saved", "actual": "500", "severity": "high"})
            video = self.cfg.runs_dir / f"s{sid}" / f"suspect-{bug}" / "bug-video.webm"
            write_under(self.tmp, video, b"webm")
            self.store.update_suspect(bug, status="confirmed", runs_failed=3, runs_total=3, video_path=str(video))
            code, out = self.req(f"/api/bugs/{bug}/jira", {})
            self.assertEqual((code, out["key"], out["attached"]), (200, "QA-7", ["bug-video.webm"]), out)
            code, out = self.req(f"/api/bugs/{bug}/jira", {})
            self.assertTrue(out["existing"])
            self.assertEqual(sum(1 for c in JiraStub.calls if c[0] == "/rest/api/3/issue"), 1)
            self.assertTrue(JiraStub.calls[0][1].startswith("Basic "))
            # the video is served with Range support so the player can seek
            code, body = fetch(urllib.request.Request(self.base + f"/media/s{sid}/suspect-{bug}/bug-video.webm",
                                                      headers={"Range": "bytes=0-1"}))
            self.assertEqual((code, body), (206, b"we"))
        finally:
            os.environ.pop("JIRA_API_TOKEN", None)
            stub.shutdown()

    def test_jira_not_configured(self):
        sid = self.new_session()
        bug = self.store.add_suspect(sid, None, {"title": "x", "url": DEMO})
        self.store.update_suspect(bug, status="confirmed")
        code, out = self.req(f"/api/bugs/{bug}/jira", {})
        self.assertEqual(code, 400)
        self.assertIn("isn't configured", out["error"])


# ---------------------------------------------------------------- real Playwright against the demo app
@unittest.skipUnless((ROOT / "node_modules" / "@playwright" / "test").exists(), "run ./install.sh first")
class TestRealRepro(Base):
    def setUp(self):
        super().setUp()
        self.demo = subprocess.Popen([sys.executable, str(ROOT / "tests" / "demo_app" / "serve.py"), "8098"],
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self.demo.stdout.readline()

    def tearDown(self):
        self.demo.terminate()
        self.demo.wait()
        super().tearDown()

    def spec(self, d: Path, body: str):
        repro.write_config(d, "chromium", "1280x800")
        write_under(self.tmp, d / "repro.spec.ts", "import { test, expect } from '@playwright/test';\n"
                                                   f"test('repro', async ({{ page }}) => {{\n{body}\n}});\n")

    def test_confirmed_with_video(self):
        d = self.tmp / "runs" / "s1" / "suspect-1"
        self.spec(d, "  await page.goto('http://localhost:8098/signup');\n"
                     "  await page.getByRole('button', { name: 'Create account' }).click();\n"
                     "  await expect(page.getByText('Name is required')).toBeVisible({ timeout: 2000 });")
        seen = []
        out = asyncio.run(repro.run_repeats(self.cfg, d, 2, on_progress=lambda *a: seen.append(a)))
        self.assertEqual(out["verdict"], "confirmed", out)
        self.assertEqual((out["failed"], out["total"]), (2, 2))
        self.assertTrue(out["video"] and Path(out["video"]).stat().st_size > 0)
        self.assertEqual(seen[-1], (2, 2, 2))

    def test_passing_spec_not_confirmed(self):
        d = self.tmp / "runs" / "s1" / "suspect-2"
        self.spec(d, "  await page.goto('http://localhost:8098/about');\n"
                     "  await expect(page.getByRole('heading', { name: 'About' })).toBeVisible();")
        out = asyncio.run(repro.run_repeats(self.cfg, d, 2))
        self.assertEqual((out["verdict"], out["failed"]), ("not_confirmed", 0), out)


if __name__ == "__main__":
    unittest.main()
