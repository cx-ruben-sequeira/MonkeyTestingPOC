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
POSIX = os.name != "nt"
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
        if POSIX:   # Windows has no owner/group/other bits; the file lives in the user's own profile
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
        self.assertIn("isn't set up", out["error"])


# ---------------------------------------------------------------- real Playwright against the demo app
@unittest.skipUnless((ROOT / "runner" / "node_modules" / "@playwright" / "test").exists(), "run mt install first")
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

    def spec(self, d: Path, body: str, imports: str = "import { test, expect, mt } from './mt';"):
        repro.write_config(d, "chromium", "1280x800", 10)
        repro.write_helper(d)
        write_under(self.tmp, d / "repro.spec.ts", imports + "\n"
                                                   f"test('repro', async ({{ page }}) => {{\n{body}\n}});\n")

    def test_confirmed_with_video(self):
        d = self.tmp / "runs" / "s1" / "suspect-1"
        self.spec(d, "  await page.goto('http://localhost:8098/signup');\n"
                     "  await mt.reached(page, /\\/signup/, 'the signup page');\n"
                     "  await mt.ready(page.getByRole('button', { name: 'Create account' }), 'the signup form');\n"
                     "  await page.getByRole('button', { name: 'Create account' }).click();\n"
                     "  await expect(page.getByText('Name is required')).toBeVisible({ timeout: 2000 });")
        seen = []
        out = asyncio.run(repro.run_repeats(self.cfg, d, 2, on_progress=lambda *a: seen.append(a)))
        self.assertEqual(out["verdict"], "confirmed", out)
        self.assertEqual((out["failed"], out["total"]), (2, 2))
        self.assertTrue(out["video"] and Path(out["video"]).stat().st_size > 0)
        self.assertEqual(seen[-1], (2, 2, 2))

    def assert_not_a_bug(self, body, why, imports=None):
        d = self.tmp / "runs" / "s1" / f"suspect-{abs(hash(body)) % 10000}"
        self.spec(d, body, *( [imports] if imports else []))
        out = asyncio.run(repro.run_repeats(self.cfg, d, 2))
        self.assertEqual(out["failed"], 2, out)                    # every run failed...
        self.assertEqual(out["verdict"], "not_confirmed", out)    # ...but that doesn't make it a bug
        self.assertIn(why, out["reason"])
        return out

    def test_never_reaching_the_page_is_not_a_bug(self):
        self.assert_not_a_bug("  await page.goto('http://localhost:8098/signup');\n"
                              "  await mt.reached(page, /\\/reports/, 'the reports page');\n"
                              "  await expect(page.getByText('nothing')).toBeVisible({ timeout: 1000 });",
                              "never reached the reports page")

    def test_content_that_never_loads_is_not_a_bug(self):
        self.assert_not_a_bug("  await page.goto('http://localhost:8098/signup');\n"
                              "  await mt.reached(page, /\\/signup/);\n"
                              "  await mt.ready(page.getByRole('listbox', { name: 'States' }), 'the state dropdown');\n"
                              "  await expect(page.getByRole('option')).toHaveCount(8);",
                              "the state dropdown never appeared")

    def test_ending_on_a_login_page_is_not_a_bug(self):
        # like the false 'custom states' bug: every step quietly skipped, the run ended on the login page
        self.assert_not_a_bug("  await page.goto('http://localhost:8098/login-organization');\n"
                              "  if (await page.getByRole('button', { name: 'State' }).isVisible().catch(() => false)) {\n"
                              "    await page.getByRole('button', { name: 'State' }).click();\n  }\n"
                              "  await mt.ready(page.locator('body'), 'the page');\n"
                              "  expect(await page.getByRole('option').count()).toBeGreaterThan(5);",
                              "ended on a login page")

    def test_no_checkpoint_cannot_confirm(self):
        self.assert_not_a_bug("  await page.goto('http://localhost:8098/signup');\n"
                              "  await page.getByRole('button', { name: 'Create account' }).click();\n"
                              "  await expect(page.getByText('Name is required')).toBeVisible({ timeout: 1000 });",
                              "never proves the page loaded", imports="import { test, expect } from '@playwright/test';")

    def test_unmet_precondition_is_not_a_bug(self):
        self.assert_not_a_bug("  await page.goto('http://localhost:8098/signup');\n"
                              "  await mt.reached(page, /\\/signup/);\n"
                              "  mt.precondition(await page.getByText('Custom states').count() > 0, 'a custom state exists');\n"
                              "  await expect(page.getByText('Custom states')).toBeVisible();",
                              "a precondition isn't met")

    def test_settled_waits_for_the_real_value(self):
        d = self.tmp / "runs" / "s1" / "suspect-77"
        self.spec(d, "  await page.setContent('<div role=\"status\" id=\"c\">0</div><script>setTimeout(() => "
                     "{ document.getElementById(\"c\").textContent = \"413,552\"; }, 1500);</script>');\n"
                     "  const text = await mt.settled(page.locator('#c'), 'the total', 1000);\n"
                     "  expect(text).toBe('93,552');")
        out = asyncio.run(repro.run_repeats(self.cfg, d, 1))
        self.assertIn("Received: \"413,552\"", out["runs"][0]["message"])   # read after it loaded, not 0

    def test_passing_spec_not_confirmed(self):
        d = self.tmp / "runs" / "s1" / "suspect-2"
        self.spec(d, "  await page.goto('http://localhost:8098/about');\n"
                     "  await expect(page.getByRole('heading', { name: 'About' })).toBeVisible();")
        out = asyncio.run(repro.run_repeats(self.cfg, d, 2))
        self.assertEqual((out["verdict"], out["failed"]), ("not_confirmed", 0), out)


# ---------------------------------------------------------------- screenshot after every action
def png_size(path) -> tuple[int, int]:
    full = os.path.normpath(os.path.abspath(os.fspath(path)))
    if not full.endswith(".png") or not os.path.isfile(full):
        raise AssertionError(f"not a PNG file: {full}")
    with open(full, "rb") as fh:
        head = fh.read(24)
    assert head[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG"
    return int.from_bytes(head[16:20], "big"), int.from_bytes(head[20:24], "big")


class TestShots(Base):
    def agent_for(self, sid, kind="test"):
        from mtlib import agents
        from mtlib.dispatcher import session_dir
        tid = self.store.add_task(sid, kind, "charter")
        sdir = session_dir(self.cfg, sid)
        ctx = agents.Context(cfg=self.cfg, store=self.store, srow=self.store.get_session(sid),
                             task=self.store.get_task(tid), task_dir=sdir / f"t{tid}", session_dir=sdir)
        return agents.make(ctx), tid

    def test_option_wires_hook_and_init_page(self):
        sid = self.new_session(shots_every_action=True)
        self.assertEqual(self.store.get_session(sid)["shots_every_action"], 1)
        agent, tid = self.agent_for(sid)
        agent.write_files()
        settings = json.loads((agent.cwd / "settings.json").read_text())
        self.assertIn("PostToolUse", settings["hooks"])
        server = json.loads((agent.cwd / "mcp.json").read_text())["mcpServers"]["playwright"]
        self.assertIn("--init-page", server["args"])
        self.assertEqual(server["env"]["MT_SHOT_DIR"], str(agent.cwd / "shots"))
        self.assertEqual(agent.env()["MT_SHOT_DIR"], str(agent.cwd / "shots"))
        self.assertTrue((agent.cwd / "shots").is_dir())

    def test_off_by_default(self):
        sid = self.new_session()
        agent, _ = self.agent_for(sid)
        agent.write_files()
        self.assertNotIn("PostToolUse", json.loads((agent.cwd / "settings.json").read_text())["hooks"])
        self.assertNotIn("--init-page", json.loads((agent.cwd / "mcp.json").read_text())["mcpServers"]["playwright"]["args"])
        self.assertNotIn("MT_SHOT_DIR", agent.env())

    def test_old_database_gets_the_column(self):
        import sqlite3
        db = self.tmp / "old" / "mt.db"
        db.parent.mkdir()
        c = sqlite3.connect(db)
        c.execute("CREATE TABLE sessions (id INTEGER PRIMARY KEY, url TEXT NOT NULL)")
        c.commit()
        c.close()
        cols = {r["name"] for r in Store(db).conn.execute("PRAGMA table_info(sessions)")}
        self.assertIn("shots_every_action", cols)


class TestShotsZip(TestServer):
    def test_zip_only_after_the_session_ends(self):
        import io
        import zipfile
        sid = self.new_session(shots_every_action=True)
        tid = self.store.add_task(sid, "test", "charter")
        shots = self.cfg.runs_dir / f"s{sid}" / f"t{tid}" / "shots"
        for i, verb in enumerate(("navigate", "click")):
            name = f"{1795000000000000000 + i}-{verb}"
            write_under(self.tmp, shots / f"{name}.png", b"\x89PNG\r\n\x1a\n" + b"\0" * 16)
            write_under(self.tmp, shots / f"{name}.json", json.dumps({"action": verb, "target": f"thing {i}"}))
        write_under(self.tmp, shots / "1795000000000000009-click.tmp.png", b"half written")
        url = self.base + f"/api/sessions/{sid}/screenshots.zip"
        self.assertEqual(fetch(urllib.request.Request(url))[0], 409)   # still queued
        self.assertEqual(self.req(f"/api/state?session={sid}")[1]["selected"]["shot_count"], 2)
        self.store.update_session(sid, status="done")
        code, raw = fetch(urllib.request.Request(url))
        self.assertEqual(code, 200)
        z = zipfile.ZipFile(io.BytesIO(raw))
        folder = f"session-{sid}-screenshots/"
        self.assertEqual(sorted(z.namelist()), sorted([folder + f"0001_t{tid}-test_navigate.png",
                                                       folder + f"0002_t{tid}-test_click.png", folder + "index.csv"]))
        self.assertIn("thing 1", z.read(folder + "index.csv").decode())
        other = self.new_session()
        self.store.update_session(other, status="done")
        self.assertEqual(fetch(urllib.request.Request(self.base + f"/api/sessions/{other}/screenshots.zip"))[0], 404)


@unittest.skipUnless((ROOT / "runner" / "node_modules" / "@playwright" / "mcp").exists(), "run mt install first")
class TestRealShots(Base):
    """The real Playwright MCP with the init-page module, driven over stdio, plus the real PostToolUse hook."""
    def setUp(self):
        super().setUp()
        self.demo = subprocess.Popen([sys.executable, str(ROOT / "tests" / "demo_app" / "serve.py"), "8097"],
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self.demo.stdout.readline()
        self.shots = self.tmp / "t1" / "shots"
        self.shots.mkdir(parents=True)
        env = dict(os.environ, MT_SHOT_DIR=str(self.shots))
        self.mcp = subprocess.Popen(
            [shutil.which("node") or "node", str(ROOT / "runner" / "node_modules" / "@playwright" / "mcp" / "cli.js"),
             "--isolated", "--headless", "--viewport-size", "1280,800", "--output-dir", str(self.tmp / "t1" / "browser"),
             "--image-responses", "omit", "--init-page", str(ROOT / "hooks" / "shot_page.js")],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env, cwd=self.tmp)
        self.next_id = 0
        self.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                "clientInfo": {"name": "mt-test", "version": "1"}})
        self.notify("notifications/initialized")

    def tearDown(self):
        self.mcp.kill()
        self.mcp.wait()
        self.demo.terminate()
        self.demo.wait()
        super().tearDown()

    def notify(self, method, params=None):
        self.mcp.stdin.write((json.dumps({"jsonrpc": "2.0", "method": method, "params": params or {}}) + "\n").encode())
        self.mcp.stdin.flush()

    def rpc(self, method, params):
        self.next_id += 1
        self.mcp.stdin.write((json.dumps({"jsonrpc": "2.0", "id": self.next_id, "method": method,
                                          "params": params}) + "\n").encode())
        self.mcp.stdin.flush()
        while True:
            msg = json.loads(self.mcp.stdout.readline())
            if msg.get("id") == self.next_id:
                return msg

    def act(self, tool, args):
        """One browser action as Claude Code runs it: the MCP tool, then the PostToolUse hook."""
        out = self.rpc("tools/call", {"name": tool, "arguments": args})
        self.assertNotIn("error", out)
        payload = json.dumps({"hook_event_name": "PostToolUse", "tool_name": "mcp__playwright__" + tool,
                              "tool_input": args})
        subprocess.run([sys.executable, str(ROOT / "hooks" / "shot.py")], input=payload.encode(), check=True,
                       env=dict(os.environ, MT_SHOT_DIR=str(self.shots)), timeout=30)

    def test_one_full_size_png_per_action(self):
        self.act("browser_navigate", {"url": "http://localhost:8097/"})
        self.act("browser_snapshot", {})            # read-only: no screenshot
        self.act("browser_navigate", {"url": "http://localhost:8097/pricing"})
        pngs = sorted(self.shots.glob("*.png"))
        self.assertEqual([p.name.split("-", 1)[1] for p in pngs], ["navigate.png", "navigate.png"])
        for p in pngs:
            self.assertEqual(png_size(p), (1280, 800))
            if POSIX:
                self.assertEqual(stat.S_IMODE(p.stat().st_mode), 0o600)
            self.assertTrue(p.with_suffix(".json").exists())
        self.assertEqual(list(self.shots.glob("*.req")) + list(self.shots.glob("*.err")), [])


class TestRepeat(Base):
    def secrets(self, sid):
        from mtlib.agents.reproducer import read_secrets
        from mtlib.dispatcher import session_dir
        return read_secrets(session_dir(self.cfg, sid))

    def test_repeat_reuses_the_saved_login(self):
        old = self.new_session(username="qa@test", password="s3cret!")
        self.store.update_session(old, status="done")
        new = self.new_session(reuse_login_from=old, repeat_of=old)
        self.assertEqual(self.store.get_session(new)["has_auth"], 1)
        self.assertEqual(self.secrets(new), {"MT_USERNAME": "qa@test", "MT_PASSWORD": "s3cret!"})
        self.assertTrue(any(f"repeat of session #{old}" in e["text"] for e in self.store.events(new, 20)))

    def test_typed_credentials_win(self):
        old = self.new_session(username="qa@test", password="s3cret!")
        new = self.new_session(reuse_login_from=old, username="other@test", password="n3w!")
        self.assertEqual(self.secrets(new)["MT_USERNAME"], "other@test")

    def test_no_saved_login(self):
        old = self.new_session()
        with self.assertRaises(sessions.FormError):
            self.new_session(reuse_login_from=old)
        with self.assertRaises(sessions.FormError):
            self.new_session(reuse_login_from=9999)


# ---------------------------------------------------------------- any OS (Windows, macOS, Linux)
def pid_alive(pid: int) -> bool:
    if os.name == "nt":
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True).stdout
        return str(pid) in out
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # a zombie child of ours counts as gone
    try:
        return os.waitpid(pid, os.WNOHANG) == (0, 0)
    except ChildProcessError:
        return True


class TestAnyOS(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="mt-os-")).resolve()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_stop_kills_the_whole_process_tree(self):
        from mtlib.worker import ClaudeRun
        argv = [Path(sys.executable).as_posix(), (ROOT / "tests" / "fake_tree.py").as_posix()]
        events = []
        run = ClaudeRun(argv, "", self.tmp, dict(os.environ), self.tmp / "stream.jsonl", lambda e, r: events.append(e))

        async def go():
            task = asyncio.create_task(run.run())
            for _ in range(200):
                if (self.tmp / "child.pid").exists():
                    break
                await asyncio.sleep(0.05)
            run.stop("cancelled")
            run_task = await asyncio.wait_for(task, 30)
            return run_task
        asyncio.run(go())
        child = int((self.tmp / "child.pid").read_text(encoding="utf-8"))
        for _ in range(100):
            if not pid_alive(child):
                break
            time.sleep(0.1)
        self.assertFalse(pid_alive(child), "the grandchild (think node/chromium) survived the stop")

    def test_program_and_path_arguments_survive_spaces(self):
        from unittest import mock
        from mtlib import worker
        with mock.patch.object(worker.shutil, "which", return_value="C:/Program Files/nodejs/node.exe"):
            self.assertEqual(worker.program("node"), "node.exe")          # found on PATH by name
        with mock.patch.object(worker.shutil, "which", return_value="/usr/bin/node"):
            self.assertEqual(worker.program("node"), "/usr/bin/node")
        with mock.patch.object(worker.shutil, "which", return_value=None):
            self.assertEqual(worker.program("node"), "node")
        spaced = self.tmp / "First Last" / "runs" / "t1"
        spaced.mkdir(parents=True)
        arg = worker.rel_arg(spaced / "settings.json", spaced)
        self.assertEqual(arg, "settings.json")
        self.assertEqual(checked_argv(["claude", "--settings", arg]), ["claude", "--settings", "settings.json"])

    def test_permission_paths_and_hook_command(self):
        from mtlib.agents import hook_command, perm_path
        self.assertEqual(perm_path(r"C:\Users\x\runs\s1"), "/c/Users/x/runs/s1")
        self.assertEqual(perm_path("D:/a/b"), "/d/a/b")
        if POSIX:
            self.assertEqual(perm_path("/srv/mt/runs"), "/srv/mt/runs")
        cmd = hook_command(ROOT / "hooks" / "guard.py")
        self.assertIn(Path(sys.executable).resolve().as_posix().split("/")[-1], cmd)
        self.assertIn("hooks/guard.py", cmd)
        self.assertNotIn("\\", cmd)

    def test_dotenv_and_data_dir(self):
        from unittest import mock
        cfg_file = self.tmp / "config.toml"
        cfg_file.write_text('[server]\nport = 8799\n', encoding="utf-8")
        (self.tmp / ".env").write_text("\ufeffexport JIRA_API_TOKEN=from-dotenv\nOTHER=ignored\n", encoding="utf-8")
        data = self.tmp / "elsewhere"
        env = {k: v for k, v in os.environ.items() if k not in ("JIRA_API_TOKEN", "MT_DATA_DIR")}
        with mock.patch.dict(os.environ, env, clear=True):
            cfg = config.load(cfg_file)
            self.assertEqual(cfg.port, 8799)
            self.assertEqual(cfg.jira_token, "from-dotenv")
            self.assertNotIn("OTHER", os.environ)
            self.assertEqual(cfg.data_dir, self.tmp)
            os.environ["MT_DATA_DIR"] = str(data)
            self.assertEqual(config.load(cfg_file).data_dir, data.resolve())
        with self.assertRaises(ValueError):
            config.read_toml(self.tmp / "config.txt")

    def test_stop_handlers_install_on_this_os(self):
        from mtlib.cli import install_stop_handlers

        async def go():
            stop = asyncio.Event()
            loop = asyncio.get_running_loop()
            done = install_stop_handlers(loop, stop)
            for sig in done:
                try:
                    loop.remove_signal_handler(sig)
                except (NotImplementedError, RuntimeError):
                    signal.signal(sig, signal.default_int_handler if sig == signal.SIGINT else signal.SIG_DFL)
            return done
        import signal
        done = asyncio.run(go())
        self.assertIn(int(signal.SIGINT), done)

    def test_doctor_reports_missing_tools(self):
        from unittest import mock
        from mtlib import setup_tools
        cfg = make_cfg(self.tmp)
        lines = []
        with mock.patch.object(setup_tools, "program", side_effect=lambda name: name):
            rc = setup_tools.doctor(cfg, out=lines.append)
        self.assertEqual(rc, 1)
        text = "\n".join(lines)
        self.assertIn("FIX   node", text)
        self.assertIn("not found on PATH", text)
        self.assertRegex(text, r"OK\s+data folder")
        self.assertTrue(all(l.split()[0] in ("OK", "WARN", "FIX", "MonkeyTesting") or l.endswith(".") for l in lines))

    def test_cli_runs_as_module(self):
        out = subprocess.run([sys.executable, "-m", "mtlib", "--help"], cwd=ROOT, capture_output=True, text=True,
                             timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        for cmd in ("serve", "install", "doctor"):
            self.assertIn(cmd, out.stdout)


# ---------------------------------------------------------------- deleting sessions
ROWS_LEFT = {
    "tasks": "SELECT COUNT(*) n FROM tasks WHERE session_id = ?",
    "pages": "SELECT COUNT(*) n FROM pages WHERE session_id = ?",
    "edges": "SELECT COUNT(*) n FROM edges WHERE session_id = ?",
    "suspects": "SELECT COUNT(*) n FROM suspects WHERE session_id = ?",
    "events": "SELECT COUNT(*) n FROM events WHERE session_id = ?",
    "usage": "SELECT COUNT(*) n FROM usage WHERE session_id = ?",
    "sessions": "SELECT COUNT(*) n FROM sessions WHERE id = ?",
}


class TestDelete(Base):
    def rows_left(self, sid):
        c = self.store.conn
        left = {t: c.execute(q, (sid,)).fetchone()["n"] for t, q in ROWS_LEFT.items()}
        return {k: v for k, v in left.items() if v}

    def test_finished_session_leaves_nothing(self):
        from mtlib import deletion
        keep = self.new_session()
        sid = self.new_session(username="qa@test", password="s3cret!")
        self.run_all()
        self.assertEqual(self.store.get_session(sid)["status"], "done")
        folder = self.cfg.runs_dir / f"s{sid}"
        self.assertTrue((folder / "secrets.env").exists())
        pre = deletion.preview(self.cfg, self.store, sid)
        self.assertGreater(pre["events"], 0)
        self.assertGreater(pre["bytes"], 0)
        self.assertFalse(pre["running"])
        out = deletion.request(self.cfg, self.store, sid)
        self.assertTrue(out["deleted"], out)
        self.assertEqual(self.rows_left(sid), {})
        self.assertFalse(folder.exists())
        self.assertTrue(self.store.get_session(keep))          # other sessions untouched
        self.assertTrue((self.cfg.runs_dir / f"s{keep}").exists())
        self.assertEqual(self.store.stats()["sessions"], 1)    # dashboard totals follow

    def test_running_session_is_cancelled_then_deleted(self):
        from mtlib import deletion
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
            out = deletion.request(self.cfg, self.store, sid)
            self.assertEqual(out["state"], "cancelling")
            end = time.time() + 30
            while time.time() < end and self.store.get_session(sid):
                disp.tick()
                await asyncio.sleep(0.05)
            return disp
        disp = asyncio.run(go())
        self.assertIsNone(self.store.get_session(sid))
        self.assertEqual(self.rows_left(sid), {})
        self.assertFalse(disp.running, "a worker was left running")
        self.assertFalse((self.cfg.runs_dir / f"s{sid}").exists())

    def test_ids_that_escape_runs_are_refused(self):
        from mtlib import deletion
        for bad in (0, -1, "../1", True):
            with self.assertRaises(ValueError):
                deletion.run_folder(self.cfg, bad)
        outside = self.tmp / "outside"
        outside.mkdir()
        (outside / "precious.txt").write_text("keep me", encoding="utf-8")
        sid = self.new_session()
        link = self.cfg.runs_dir / f"s{sid}"
        shutil.rmtree(link)
        try:
            os.symlink(outside, link, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("this OS/user can't create symlinks")
        with self.assertRaises(ValueError):
            deletion.request(self.cfg, self.store, sid)
        self.assertTrue((outside / "precious.txt").exists())
        self.assertTrue(self.store.get_session(sid))

    def test_read_only_files_are_deleted(self):
        from mtlib import deletion
        sid = self.new_session()
        self.store.update_session(sid, status="done")
        folder = self.cfg.runs_dir / f"s{sid}"
        clone = folder / "t9" / "clone" / ".git" / "objects"
        clone.mkdir(parents=True)
        for i in range(3):
            f = clone / f"pack{i}.idx"
            f.write_bytes(b"x" * 100)
            os.chmod(f, stat.S_IREAD)
        os.chmod(clone, stat.S_IREAD | stat.S_IEXEC)
        self.assertTrue(deletion.request(self.cfg, self.store, sid)["deleted"])
        self.assertFalse(folder.exists())

    def test_incomplete_delete_keeps_rows(self):
        from unittest import mock
        from mtlib import deletion
        sid = self.new_session(username="qa@test", password="s3cret!")
        self.store.update_session(sid, status="cancelled")
        self.assertTrue((self.cfg.runs_dir / f"s{sid}" / "secrets.env").exists())
        with mock.patch.object(deletion, "remove_tree", return_value=["/locked/file"]):
            out = deletion.request(self.cfg, self.store, sid)
        self.assertEqual(out["state"], "incomplete")
        self.assertEqual(self.store.get_session(sid)["delete_state"], "incomplete")
        self.assertFalse((self.cfg.runs_dir / f"s{sid}" / "secrets.env").exists())
        self.assertTrue(deletion.request(self.cfg, self.store, sid)["deleted"])   # retry works

    def test_filters(self):
        from mtlib import deletion
        old, new, running = self.new_session(), self.new_session(), self.new_session()
        now = time.time()
        self.store.update_session(old, status="done", finished_at=now - 40 * 86400)
        self.store.update_session(new, status="cancelled", finished_at=now - 3600)
        self.store.update_session(running, status="running")
        ids = lambda rows: sorted(int(r["id"]) for r in rows)
        self.assertEqual(ids(deletion.matching(self.store, 30 * 86400)), [old])
        self.assertEqual(ids(deletion.matching(self.store, None, "finished")), [old, new])
        self.assertEqual(ids(deletion.matching(self.store, None, "cancelled")), [new])

    def test_cli(self):
        from mtlib import cli
        a, b = self.new_session(), self.new_session()
        self.store.update_session(a, status="done", finished_at=time.time() - 90 * 86400)
        self.store.update_session(b, status="done", finished_at=time.time())
        cfg_file = self.tmp / "config.toml"
        cfg_file.write_text(f"[paths]\ndata_dir = {json.dumps(Path(self.tmp).as_posix())}\n", encoding="utf-8")
        from unittest import mock
        with mock.patch("builtins.input", return_value="n"):
            self.assertEqual(cli.main(["--config", str(cfg_file), "delete", str(a)]), 1)   # answered no
        self.assertTrue(self.store.get_session(a))
        self.assertEqual(cli.main(["--config", str(cfg_file), "delete", "--older-than", "30d", "--yes"]), 0)
        self.assertIsNone(self.store.get_session(a))
        self.assertTrue(self.store.get_session(b))
        self.assertEqual(cli.main(["--config", str(cfg_file), "delete", "999", "--yes"]), 1)


class TestDeleteApi(TestServer):
    def call(self, method, path, body=None, headers=None):
        h = {"X-MT": "1", "Content-Type": "application/json"}
        h.update(headers or {})
        r = urllib.request.Request(self.base + path, data=json.dumps(body).encode() if body is not None else None,
                                   headers=h, method=method)
        code, raw = fetch(r)
        return code, json.loads(raw or b"null")

    def test_preview_delete_and_bulk(self):
        a, b, c = (self.new_session() for _ in range(3))
        for sid in (a, b, c):
            self.store.update_session(sid, status="done")
        code, pre = self.call("GET", f"/api/sessions/{a}/delete-preview")
        self.assertEqual(code, 200)
        self.assertEqual(pre["session"]["id"], a)
        self.assertIn("bytes", pre)
        self.assertEqual(self.call("DELETE", f"/api/sessions/{a}", headers={"X-MT": "0"})[0], 403)
        self.assertEqual(self.call("DELETE", f"/api/sessions/{a}", headers={"Origin": "http://evil.test"})[0], 403)
        code, out = self.call("DELETE", f"/api/sessions/{a}")
        self.assertEqual((code, out["deleted"]), (200, True))
        self.assertEqual(self.call("DELETE", f"/api/sessions/{a}")[0], 404)
        code, out = self.call("POST", "/api/sessions/delete", {"ids": [b, c, 9999]})
        self.assertEqual((code, out["deleted"]), (200, 2))
        self.assertEqual(self.call("POST", "/api/sessions/delete", {"ids": "1"})[0], 400)
        self.assertEqual(self.store.stats()["sessions"], 0)


# ---------------------------------------------------------------- page load wait
class TestPageLoad(Base):
    def agent_for(self, sid, kind="test"):
        from mtlib import agents
        from mtlib.dispatcher import session_dir
        tid = self.store.add_task(sid, kind, "charter")
        sdir = session_dir(self.cfg, sid)
        ctx = agents.Context(cfg=self.cfg, store=self.store, srow=self.store.get_session(sid),
                             task=self.store.get_task(tid), task_dir=sdir / f"t{tid}", session_dir=sdir)
        return agents.make(ctx)

    def test_default_and_limits(self):
        self.assertEqual(self.store.get_session(self.new_session())["page_load_s"], 120)
        self.assertEqual(self.store.get_session(self.new_session(page_load_s=30))["page_load_s"], 30)
        for bad in (4, 601, "slow"):
            with self.assertRaises(sessions.FormError):
                self.new_session(page_load_s=bad)

    def test_browser_waits_that_long(self):
        agent = self.agent_for(self.new_session(page_load_s=30))
        agent.write_files()
        args = json.loads((agent.cwd / "mcp.json").read_text(encoding="utf-8"))["mcpServers"]["playwright"]["args"]
        self.assertEqual(args[args.index("--timeout-navigation") + 1], "30000")
        system = (agent.cwd / "append.txt").read_text(encoding="utf-8")
        self.assertIn("up to 30 seconds to load", system)
        self.assertNotIn("$page_load_s", system + agent.prompt())

    def test_reproduction_config_and_budget(self):
        d = self.tmp / "runs" / "s1" / "suspect-1"
        text = repro.write_config(d, "chromium", "1280x800", 30).read_text(encoding="utf-8")
        self.assertIn("navigationTimeout: 30000", text)
        self.assertIn("expect: { timeout: 30000 }", text)
        self.assertIn(f"timeout: {repro.test_timeout_s(30) * 1000},", text)
        self.assertIn("navigationTimeout: 120000", repro.write_config(d, "chromium", "1280x800").read_text(encoding="utf-8"))


# ---------------------------------------------------------------- the user's instructions, followed strictly
class TestInstructions(Base):
    def test_fallback_parser(self):
        from mtlib import rules
        r = rules.parse_fallback("after you login make your tests under https://app.test/riskhub. "
                                 "Don't go to /riskhub/admin or https://other.test/x", "https://app.test/",
                                 ["https://app.test"])
        self.assertEqual(r["focus_urls"], ["https://app.test/riskhub"])
        self.assertEqual(r["scope"], "below")
        self.assertEqual(r["exclude_paths"], ["/riskhub/admin"])
        self.assertEqual(rules.parse_fallback("test only this page: /settings", "https://app.test/",
                                              ["https://app.test"])["scope"], "page")
        bad = rules.normalize({"focus_urls": ["https://evil.test/x"], "scope": "nonsense"}, "https://app.test/",
                              ["https://app.test"])
        self.assertEqual((bad["focus_urls"], bad["scope"]), ([], "app"))

    def test_pipeline_stays_in_the_focus_area(self):
        instr = f"After you login make your tests under {DEMO}/reports. Never click export. Use the name Ana."
        sid = self.new_session(instructions=instr)
        self.run_all()
        s = self.store.get_session(sid)
        tasks = self.store.tasks_for(sid)
        self.assertEqual(tasks[0]["kind"], "rules")                      # the interpreter ran first
        self.assertEqual(s["rules"]["source"], "interpreter")
        self.assertEqual(s["rules"]["focus_urls"], [f"{DEMO}/reports"])
        self.assertIn("export", s["rules"]["forbidden_actions"])
        guard_rules = json.loads((self.cfg.runs_dir / f"s{sid}" / "guard.json").read_text(encoding="utf-8"))
        self.assertEqual(guard_rules["scope"]["focus"], [f"{DEMO}/reports"])
        self.assertEqual(guard_rules["forbidden"], ["export"])
        for t in tasks:                                                   # every charter starts inside the focus
            if t["kind"] == "test":
                self.assertTrue(t["charter"]["start_url"].startswith(f"{DEMO}/reports"), t["charter"])
        visited = [p["url"] for p in self.store.pages(sid) if p["visited"]]
        self.assertTrue(visited)
        self.assertTrue(all(u.startswith(f"{DEMO}/reports") for u in visited), visited)
        titles = [x["title"] for x in self.store.suspects(sid)]
        self.assertNotIn("Signup accepts an empty name", titles)          # found outside the focus: dropped
        self.assertIn("Generate report returns 500", titles)
        explore_dir = next(self.cfg.runs_dir.glob(f"s{sid}/t*/append.txt"))
        system = explore_dir.read_text(encoding="utf-8")
        self.assertIn("USER INSTRUCTIONS - BINDING", system)
        self.assertIn(instr, system)
        self.assertIn("MUST: use the name Ana", system)
        self.assertIn(f"Focus ONLY on: {DEMO}/reports", system)

    def test_interpreter_failure_falls_back(self):
        os.environ["FAKE_CLAUDE_MODE"] = "rules_fail"
        sid = self.new_session(instructions=f"Only test {DEMO}/reports please. Don't touch /admin.")
        self.run_all()
        r = self.store.get_session(sid)["rules"]
        self.assertEqual(r["source"], "parser")
        self.assertEqual(r["focus_urls"], [f"{DEMO}/reports"])
        self.assertEqual(r["exclude_paths"], ["/admin"])

    def test_landing_outside_sends_the_worker_back(self):
        from mtlib import agents
        from mtlib.dispatcher import session_dir
        sid = self.new_session()
        self.store.update_session(sid, rules={"focus_urls": [f"{DEMO}/reports"], "scope": "below", "source": "interpreter"})
        from mtlib.sessions import write_guard
        write_guard(self.cfg, self.store.get_session(sid))
        tid = self.store.add_task(sid, "test", "c")
        sdir = session_dir(self.cfg, sid)
        ag = agents.make(agents.Context(cfg=self.cfg, store=self.store, srow=self.store.get_session(sid),
                                        task=self.store.get_task(tid), task_dir=sdir / f"t{tid}", session_dir=sdir))
        (sdir / f"t{tid}").mkdir(parents=True, exist_ok=True)
        ag.write_files()                      # no login in this session: the rules apply from the start
        self.assertTrue(ag.logged_in)
        ag.on_page(f"{DEMO}/pricing")
        self.assertTrue(ag.scope_flag.exists())
        env = dict(os.environ, **ag.env())

        def hook(tool, args):
            out = subprocess.run([sys.executable, str(ROOT / "hooks" / "guard.py")], env=env, capture_output=True,
                                 text=True, input=json.dumps({"tool_name": "mcp__playwright__browser_" + tool,
                                                              "tool_input": args}))
            return out.stdout
        self.assertIn("deny", hook("click", {"element": "Buy button"}))
        self.assertIn("go back", hook("click", {"element": "Buy button"}).lower())
        self.assertEqual(hook("navigate_back", {}), "")
        self.assertEqual(hook("navigate", {"url": f"{DEMO}/reports"}), "")
        self.assertIn("deny", hook("navigate", {"url": f"{DEMO}/pricing"}))
        ag.on_page(f"{DEMO}/reports/2026")
        self.assertFalse(ag.scope_flag.exists())
        self.assertEqual(hook("click", {"element": "Generate report"}), "")


# ---------------------------------------------------------------- Jira connection and tickets in instructions
TICKET = {"key": "AST-12", "fields": {
    "summary": "Risk Hub filters lose their values", "issuetype": {"name": "Story"}, "status": {"name": "In Progress"},
    "description": {"type": "doc", "version": 1, "content": [
        {"type": "paragraph", "content": [{"type": "text", "text": "Test the Risk Hub filters on "},
                                          {"type": "text", "text": "/reports"}]},
        {"type": "bulletList", "content": [{"type": "listItem", "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": "Filters keep their values after reload"}]}]}]}]},
    "customfield_100": {"type": "doc", "version": 1, "content": [
        {"type": "paragraph", "content": [{"type": "text", "text": "Given a filter, when I reload, then it stays"}]}]},
    "comment": {"comments": [{"author": {"displayName": "Dana"}, "body": {"type": "doc", "version": 1, "content": [
        {"type": "paragraph", "content": [{"type": "text", "text": "Also check the date picker"}]}]}}]}},
    "names": {"customfield_100": "Acceptance Criteria", "summary": "Summary"}}


class TicketStub(BaseHTTPRequestHandler):
    seen: list = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        TicketStub.seen.append((self.path, self.headers.get("Authorization")))
        if self.path.startswith("/rest/api/3/issue/AST-12"):
            code, out = 200, TICKET
        elif self.path == "/rest/api/3/myself":
            code, out = 200, {"displayName": "QA Bot", "emailAddress": "qa@test"}
        else:
            code, out = 404, {"errorMessages": ["Issue does not exist or you do not have permission to see it."]}
        raw = json.dumps(out).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


class TestJiraTickets(Base):
    def setUp(self):
        super().setUp()
        TicketStub.seen = []
        self.stub = ThreadingHTTPServer(("127.0.0.1", 0), TicketStub)
        threading.Thread(target=self.stub.serve_forever, daemon=True).start()

    def tearDown(self):
        self.stub.shutdown()
        super().tearDown()

    def connect(self):
        from mtlib import jira_settings
        self.cfg.jira_base_url = f"http://127.0.0.1:{self.stub.server_address[1]}"
        self.cfg.jira_email = "qa@test"
        jira_settings.set_token(self.cfg, "test-token-1234567890")

    def test_keys_and_adf(self):
        from mtlib import jira
        self.assertEqual(jira.ticket_keys("see AST-12, https://x.atlassian.net/browse/RISK-7 and UTF-8, CVE-2024"),
                         ["AST-12", "RISK-7"])
        text = jira.adf_to_text(TICKET["fields"]["description"])
        self.assertIn("Test the Risk Hub filters on /reports", text)
        self.assertIn("- Filters keep their values after reload", text)

    def test_get_issue_reads_description_criteria_and_optional_comments(self):
        from mtlib import jira
        self.connect()
        t = jira.get_issue(self.cfg, "AST-12")
        self.assertEqual((t["summary"], t["status"], t["type"]), ("Risk Hub filters lose their values", "In Progress", "Story"))
        self.assertIn("Acceptance Criteria:\nGiven a filter", t["text"])
        self.assertNotIn("date picker", t["text"])
        self.assertIn("Dana: Also check the date picker", jira.get_issue(self.cfg, "AST-12", comments=True)["text"])
        self.assertTrue(TicketStub.seen[0][1].startswith("Basic "))
        bad = jira.read_tickets(self.cfg, "and AST-99 too")
        self.assertIn("404", bad[0]["error"])

    def test_tickets_reach_every_worker(self):
        self.connect()
        sid = self.new_session(instructions="Test what AST-12 asks for.")
        self.run_all()
        s = self.store.get_session(sid)
        self.assertEqual([t["key"] for t in s["tickets"]], ["AST-12"])
        self.assertFalse(s["tickets"][0].get("error"))
        prompts = [p.read_text(encoding="utf-8") for p in self.cfg.runs_dir.glob(f"s{sid}/t*/append.txt")]
        self.assertTrue(prompts)
        for text in prompts:
            self.assertIn("AST-12 [Story, In Progress]: Risk Hub filters lose their values", text)
            self.assertIn("Given a filter, when I reload, then it stays", text)
        self.assertTrue(any("Jira AST-12 read" in e["text"] for e in self.store.events(sid, 200)))

    def test_without_a_connection_the_session_says_so(self):
        sid = self.new_session(instructions="Test what AST-12 asks for.")
        self.run_all()
        t = self.store.get_session(sid)["tickets"][0]
        self.assertIn("Jira connection", t["error"])
        text = next(self.cfg.runs_dir.glob(f"s{sid}/t*/append.txt")).read_text(encoding="utf-8")
        self.assertIn("AST-12: could not be read", text)
        self.assertIn("Don't guess what it says", text)


class TestJiraSettingsApi(TestServer):
    def call(self, path, body=None, headers=None):
        h = {"X-MT": "1", "Content-Type": "application/json"}
        h.update(headers or {})
        r = urllib.request.Request(self.base + path, data=json.dumps(body).encode() if body is not None else None,
                                   headers=h, method="POST" if body is not None else "GET")
        code, raw = fetch(r)
        return code, json.loads(raw or b"null")

    def test_token_is_write_only_and_never_stored(self):
        token = "ATATT-test-token-ABCDEFGHIJ1234567890"
        form = {"base_url": "https://example.atlassian.net/", "email": "qa@example.com", "project_key": "qa", "token": token}
        self.assertEqual(self.call("/api/jira/settings", form, {"X-MT": "0"})[0], 403)
        code, out = self.call("/api/jira/settings", form)
        self.assertEqual(code, 200, out)
        self.assertEqual((out["base_url"], out["project_key"], out["token_saved"], out["ready"]),
                         ("https://example.atlassian.net", "QA", True, True))
        code, got = self.call("/api/jira/settings")
        self.assertNotIn(token, json.dumps(got))
        self.assertNotIn(token, json.dumps(self.call("/api/state")[1]))
        self.assertNotIn(token, repr(self.cfg))
        db_bytes = b"".join(p.read_bytes() for p in self.cfg.db_path.parent.glob("mt.db*"))
        self.assertNotIn(token.encode(), db_bytes)
        self.assertFalse(any(token in p.read_text(errors="ignore") for p in self.cfg.data_dir.rglob("*") if p.is_file()
                             and p.suffix not in (".db", ".db-wal", ".db-shm")))
        code, out = self.call("/api/jira/settings", dict(form, token="", clear_token=True))
        self.assertFalse(out["token_saved"])
        for bad in ({"base_url": "http://plain.test"}, {"email": "nope"}, {"project_key": "bad key"},
                    {"token": "short"}):
            self.assertEqual(self.call("/api/jira/settings", dict(form, **bad))[0], 400, bad)

    def test_connection_test(self):
        from mtlib import jira_settings
        stub = ThreadingHTTPServer(("127.0.0.1", 0), TicketStub)
        threading.Thread(target=stub.serve_forever, daemon=True).start()
        try:
            self.assertEqual(self.call("/api/jira/test", {})[0], 400)
            self.cfg.jira_base_url = f"http://127.0.0.1:{stub.server_address[1]}"
            self.cfg.jira_email = "qa@test"
            jira_settings.set_token(self.cfg, "test-token-1234567890")
            code, out = self.call("/api/jira/test", {})
            self.assertEqual((code, out["name"]), (200, "QA Bot"))
        finally:
            stub.shutdown()


class TestLoginFirst(Base):
    def run_session(self, mode=None):
        if mode:
            os.environ["FAKE_CLAUDE_MODE"] = mode
        sid = self.new_session(username="qa@test", password="s3cret!",
                               instructions=f"After you log in, make your tests under {DEMO}/reports only.")
        self.run_all()
        return sid

    def check(self, sid, how):
        events = [e["text"] for e in self.store.events(sid, 500)]
        self.assertFalse([t for t in events if "login-organization" in t and "left the focus area" in t], events)
        self.assertFalse([t for t in events if "guard blocked" in t and "outside the area" in t and "login" in t])
        self.assertTrue(any(f"logged in ({how}" in t for t in events), events)
        pages = {p["url"]: p["visited"] for p in self.store.pages(sid)}
        self.assertFalse(pages.get(f"{DEMO}/login-organization"))      # recorded, never tested
        self.assertTrue(pages.get(f"{DEMO}/reports"))
        self.assertIn("Generate report returns 500", [x["title"] for x in self.store.suspects(sid)])
        system = next(self.cfg.runs_dir.glob(f"s{sid}/t*/append.txt")).read_text(encoding="utf-8")
        self.assertLess(system.index("LOGIN FIRST"), system.index("USER INSTRUCTIONS - BINDING (once you are logged in)"))

    def test_login_then_instructions(self):
        self.check(self.run_session(), "")   # the page change or the LOGGED_IN line, whichever comes first

    def test_login_detected_without_the_marker(self):
        self.check(self.run_session("login_auto"), "left the login pages")

    def test_guard_ignores_focus_until_logged_in(self):
        sid = self.new_session(username="qa@test", password="s3cret!")
        self.store.update_session(sid, rules={"focus_urls": [f"{DEMO}/reports"], "scope": "below", "source": "interpreter"})
        from mtlib.sessions import write_guard
        write_guard(self.cfg, self.store.get_session(sid))
        flag = self.tmp / "t1" / "loggedin.flag"
        flag.parent.mkdir(parents=True)
        env = dict(os.environ, MT_GUARD_FILE=str(self.cfg.runs_dir / f"s{sid}" / "guard.json"), MT_LOGIN_FILE=str(flag))

        def nav(url):
            return subprocess.run([sys.executable, str(ROOT / "hooks" / "guard.py")], env=env, capture_output=True,
                                  text=True, input=json.dumps({"tool_name": "mcp__playwright__browser_navigate",
                                                               "tool_input": {"url": url}})).stdout
        self.assertEqual(nav(f"{DEMO}/"), "")                    # before login: the landing page is fine
        self.assertIn("deny", nav("http://evil.test/"))           # other sites never are
        flag.write_text("x", encoding="utf-8")
        self.assertIn("deny", nav(f"{DEMO}/pricing"))             # after login: focus rules apply
        self.assertEqual(nav(f"{DEMO}/login-organization"), "")   # and login pages stay allowed


# ---------------------------------------------------------------- a failure must match the reported bug
class TestReportedValue(Base):
    def test_mismatch_rules(self):
        miss = repro.received_mismatch
        self.assertIn("empty value", miss("Expected: 93552\nReceived: 0", "Tab header shows (413,552)"))
        self.assertIn("bug reports 413552", miss("Expected: 93552\nReceived: 120", "Tab header shows (413,552)"))
        self.assertIsNone(miss("Expected: 93552\nReceived: 413552", "Tab header shows (413,552)"))
        self.assertIsNone(miss("Expected: < 500\nReceived:   500", "GET /api/report -> 500"))
        self.assertIsNone(miss("Expected: visible\nReceived: hidden", "the error message is missing"))
        self.assertIsNone(miss('Expected: "Saved"\nReceived: ""', "the toast is empty after saving"))

    def test_reproducer_does_not_confirm_a_different_failure(self):
        from mtlib import agents
        from mtlib.dispatcher import session_dir
        sid = self.new_session()
        sus = self.store.add_suspect(sid, None, {"title": "Count is wrong", "url": DEMO + "/reports",
                                                 "actual": "Tab header shows (413,552)", "expected": "93,552"})
        tid = self.store.add_task(sid, "repro", "r", suspect_id=sus)

        async def got_zero(cfg, work_dir, n, env_extra=None, on_progress=None, timeout_s=None):
            return {"verdict": "confirmed", "reason": "failed 3/3 on the same assertion", "total": 3, "failed": 3,
                    "runs": [{"i": i, "status": "failed", "message": "Expected: 93552\nReceived: 0"} for i in (1, 2, 3)]}
        sdir = session_dir(self.cfg, sid)
        ag = agents.make(agents.Context(cfg=self.cfg, store=self.store, srow=self.store.get_session(sid),
                                        task=self.store.get_task(tid), task_dir=sdir / f"t{tid}", session_dir=sdir,
                                        repro_fn=got_zero))
        asyncio.run(ag.finish(None, None))
        s = self.store.get_suspect(sus)
        self.assertEqual(s["status"], "not_confirmed")
        self.assertIn("read 0", s["reason"])

    def test_charter_expectations_are_dropped(self):
        from mtlib import agents
        from mtlib.dispatcher import session_dir
        sid = self.new_session()
        tid = self.store.add_task(sid, "test", "c")
        sdir = session_dir(self.cfg, sid)
        ag = agents.make(agents.Context(cfg=self.cfg, store=self.store, srow=self.store.get_session(sid),
                                        task=self.store.get_task(tid), task_dir=sdir / f"t{tid}", session_dir=sdir))
        self.assertIsNone(ag.add_suspect({"title": "Count differs", "url": DEMO + "/", "actual": "413,552",
                                          "expected": "Tab count should match charter specification of 93,552"}))
        self.assertIsNotNone(ag.add_suspect({"title": "Badge and list disagree", "url": DEMO + "/", "actual": "120 vs 80",
                                             "expected": "the badge count equals the rows in the list"}))

    def test_videos_record_at_full_size(self):
        d = self.tmp / "runs" / "s1" / "suspect-9"
        text = repro.write_config(d, "chromium", "1920x1080").read_text(encoding="utf-8")
        self.assertIn("viewport: { width: 1920, height: 1080 }", text)
        self.assertIn("video: { mode: 'on', size: { width: 1920, height: 1080 } }", text)
        self.assertEqual(self.store.get_session(self.new_session())["viewport"], "1920x1080")


if __name__ == "__main__":
    unittest.main()
