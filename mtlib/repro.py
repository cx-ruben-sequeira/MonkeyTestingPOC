"""Runs a suspect's repro.spec.ts N times with Playwright Test and reads the JSON report itself.

The confirmation rule lives here, not in any agent: a suspect is a Bug only when every one of the
N runs failed, and all failures are the same expect() assertion.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import shlex
import shutil
from pathlib import Path

from .config import Config
from . import proc as procs
from .worker import SECRET_ENV, checked_argv, program, rel_arg

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

CONFIG_TS = """import { defineConfig } from '@playwright/test';

// Written by MonkeyTesting for this suspect. Video and screenshots on for every run.
export default defineConfig({
  testDir: '.',
  testMatch: 'repro.spec.ts',
  timeout: __TEST_TIMEOUT__,
  expect: { timeout: __EXPECT_TIMEOUT__ },
  retries: 0,
  workers: 1,
  outputDir: 'test-results',
  reporter: 'line',
  use: {
    browserName: '__BROWSER__',__CHANNEL__
    headless: true,
    viewport: { width: __W__, height: __H__ },
    video: 'on',
    screenshot: 'on',
    trace: 'retain-on-failure',
    actionTimeout: 10000,
    navigationTimeout: __NAV_TIMEOUT__,
  },
});
"""


def _dir(path) -> str:
    return os.path.normpath(os.path.abspath(os.fspath(path)))


DEFAULT_PAGE_LOAD_S = 120


def test_timeout_s(page_load_s: int) -> int:
    """Room for one run: a slow first load, a slow assertion wait, plus the steps themselves."""
    return 2 * int(page_load_s) + 60


def write_config(work_dir, browser: str, viewport: str, page_load_s: int = DEFAULT_PAGE_LOAD_S) -> Path:
    """playwright.config.ts for one suspect. page_load_s is how long a page may take to load: it is the
    navigation timeout and the expect() wait, so a slow page is not mistaken for a broken one."""
    base = _dir(work_dir)
    os.makedirs(base, exist_ok=True)
    browser = (browser or "chromium").lower()
    channel = ""
    if browser in ("chrome", "msedge"):
        channel, browser = f"\n    channel: '{browser}',", "chromium"
    if browser not in ("chromium", "firefox", "webkit"):
        browser = "chromium"
    m = re.match(r"^(\d{3,4})x(\d{3,4})$", viewport or "")
    w, h = (m.group(1), m.group(2)) if m else ("1280", "800")
    text = (CONFIG_TS.replace("__BROWSER__", browser).replace("__CHANNEL__", channel)
            .replace("__W__", w).replace("__H__", h))
    load_ms = max(5, min(600, int(page_load_s or DEFAULT_PAGE_LOAD_S))) * 1000
    text = (text.replace("__TEST_TIMEOUT__", str(test_timeout_s(load_ms // 1000) * 1000))
            .replace("__EXPECT_TIMEOUT__", str(load_ms)).replace("__NAV_TIMEOUT__", str(load_ms)))
    path = os.path.normpath(os.path.join(base, "playwright.config.ts"))
    if os.path.dirname(path) != base:
        raise ValueError("bad config path")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return Path(path)


def signature(message: str) -> str:
    """What makes two failures 'the same assertion': first error line, numbers folded."""
    first = ANSI_RE.sub("", message or "").strip().splitlines()[:1]
    line = first[0] if first else ""
    line = re.sub(r"\d+(\.\d+)?", "#", line)
    return " ".join(line.split())[:240]


def is_assertion(message: str) -> bool:
    m = ANSI_RE.sub("", message or "")
    return "expect(" in m or "toBe" in m or "toHave" in m or "AssertionError" in m


def collect_results(report: dict) -> list[dict]:
    """Every test result in a Playwright JSON report, in run order."""
    out: list[dict] = []

    def walk(suite: dict) -> None:
        for spec in suite.get("specs") or []:
            for test in spec.get("tests") or []:
                for r in test.get("results") or []:
                    err = r.get("error") or {}
                    msg = err.get("message") or "\n".join(e.get("message", "") for e in r.get("errors") or [])
                    final_state = {}
                    for a in r.get("attachments") or []:
                        if a.get("name") == "mt-final-state" and a.get("body"):
                            try:
                                final_state = json.loads(base64.b64decode(a["body"]).decode("utf-8"))
                            except (ValueError, TypeError):
                                final_state = {}
                    out.append({"status": r.get("status"), "message": ANSI_RE.sub("", msg or "")[:2000],
                                "duration_ms": r.get("duration"), "final_state": final_state,
                                "attachments": [{"name": a.get("name"), "path": a.get("path"),
                                                 "type": a.get("contentType")}
                                                for a in r.get("attachments") or [] if a.get("path")]})
        for child in suite.get("suites") or []:
            walk(child)

    for s in report.get("suites") or []:
        walk(s)
    return out


MT_TS = """// Written by MonkeyTesting next to every reproduction. repro.spec.ts imports `test` and `expect` from here.
// - An automatic fixture records where each run really ended (URL, login page or not), so a run that never
//   logged in or never reached the bug can't count as reproducing it.
// - mt.reached / mt.ready are checkpoints: they prove the bug's page and content loaded before the bug is
//   asserted. A failure there means "never got to the bug", not "bug confirmed".
// - mt.precondition states the data or settings the bug depends on (e.g. "a custom state exists").
import { test as base, expect, Page, Locator } from '@playwright/test';

const AUTH = new RegExp(__AUTH_RE__, 'i');
export { expect };

export const test = base.extend<{ mtFinalState: void }>({
  mtFinalState: [async ({ page }, use, testInfo) => {
    await use();
    let url = '';
    try { url = page.url(); } catch { url = ''; }
    let path = '';
    try { path = new URL(url).pathname; } catch { path = ''; }
    let passwordField = false;
    try { passwordField = await page.locator('input[type="password"]').first().isVisible(); } catch { passwordField = false; }
    await testInfo.attach('mt-final-state', {
      body: JSON.stringify({ url, path, onLoginPage: AUTH.test(path) || passwordField }),
      contentType: 'application/json',
    });
  }, { auto: true }],
});

export const mt = {
  /** The browser is on the bug's page (and not on a login page). */
  async reached(page: Page, url: RegExp | string, what = 'the page') {
    try {
      await expect(page).toHaveURL(url);
    } catch (e) {
      throw new Error(`MT-SETUP: never reached ${what} (ended on ${page.url()})`);
    }
    let path = '';
    try { path = new URL(page.url()).pathname; } catch { path = ''; }
    if (AUTH.test(path)) throw new Error(`MT-SETUP: still on a login page (${page.url()})`);
  },
  /** The content the bug is about has loaded (a list, a dropdown, a form...). */
  async ready(locator: Locator, what = 'the content') {
    try {
      await expect(locator.first()).toBeVisible();
    } catch (e) {
      throw new Error(`MT-SETUP: ${what} never appeared`);
    }
  },
  /** Something the bug depends on must be true first (e.g. the data or setting exists). */
  precondition(ok: boolean, what: string) {
    if (!ok) throw new Error(`MT-PRECONDITION: ${what}`);
  },
};
"""
CHECKPOINT_RE = re.compile(r"\bmt\.(reached|ready)\s*\(")
IMPORT_RE = re.compile(r"""from\s+['"]@playwright/test['"]""")


def write_helper(work_dir) -> Path:
    """mt.ts next to the spec (always rewritten, so a reproducer can't weaken it)."""
    from .scope import AUTH_PATH_RE
    base = _dir(work_dir)
    path = os.path.normpath(os.path.join(base, "mt.ts"))
    if os.path.dirname(path) != base:
        raise ValueError("bad helper path")
    text = MT_TS.replace("__AUTH_RE__", json.dumps(AUTH_PATH_RE.pattern))
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return Path(path)


def use_helper(spec_path) -> str:
    """Makes the spec import test/expect from ./mt (so its runs are checked), and returns its text."""
    full = os.path.normpath(os.path.abspath(os.fspath(spec_path)))
    if not full.endswith("repro.spec.ts") or not os.path.isfile(full):
        return ""
    with open(full, "r", encoding="utf-8") as fh:
        text = fh.read()
    fixed = IMPORT_RE.sub("from './mt'", text)
    if fixed != text:
        with open(full, "w", encoding="utf-8") as fh:
            fh.write(fixed)
    return fixed
SETUP_RE = re.compile(r"MT-(SETUP|PRECONDITION):\s*([^\n]{0,240})")


def invalid_reason(r: dict) -> str | None:
    """Why a failed run doesn't count as reproducing the bug: it failed before reaching it (a checkpoint or a
    precondition failed) or it ended on a login page. None when the failure is a real one."""
    if r["status"] not in ("failed", "timedOut"):
        return None
    m = SETUP_RE.search(r.get("message") or "")
    if m:
        kind = "a precondition isn't met" if m.group(1) == "PRECONDITION" else "it never reached the bug"
        return f"{kind}: {m.group(2).strip()}"
    fs = r.get("final_state") or {}
    if fs.get("onLoginPage"):
        return f"it ended on a login page ({fs.get('url') or fs.get('path')}): the test never logged in"
    return None


def decide(results: list[dict], n: int, spec_text: str | None = None) -> dict:
    """Applies the N-of-N rule to parsed results. A run only counts as reproducing the bug when it got past
    the spec's checkpoints (mt.reached / mt.ready) and didn't end on a login page; a spec without any
    checkpoint can't confirm a bug, because nothing proves the page loaded."""
    failed = [r for r in results if r["status"] in ("failed", "timedOut")]
    sigs = {signature(r["message"]) for r in failed}
    total = len(results)
    invalid = [(r, invalid_reason(r)) for r in failed]
    invalid = [(r, why) for r, why in invalid if why]
    if total >= n and invalid:
        return {"verdict": "not_confirmed", "invalid": True, "total": total, "failed": len(failed),
                "reason": f"not a bug: {len(invalid)}/{total} runs failed before reaching it, because {invalid[0][1]}",
                "signature": None}
    if total >= n and spec_text is not None and not CHECKPOINT_RE.search(spec_text) and failed:
        return {"verdict": "not_confirmed", "invalid": True, "total": total, "failed": len(failed),
                "reason": "the test never proves the page loaded (no mt.reached / mt.ready checkpoint before the "
                          "assertion), so its failures can't confirm a bug", "signature": None}
    if total < n:
        verdict, reason = "error", f"only {total} of {n} runs reported"
    elif len(failed) == total and len(sigs) == 1 and all(is_assertion(r["message"]) for r in failed):
        verdict, reason = "confirmed", f"failed {total}/{total} on the same assertion"
    elif len(failed) == total and len(sigs) == 1:
        verdict, reason = "not_confirmed", f"failed {total}/{total} but not on an assertion (spec problem?)"
    elif len(failed) == total:
        verdict, reason = "not_confirmed", f"failed {total}/{total} with {len(sigs)} different errors"
    else:
        verdict, reason = "not_confirmed", f"failed {len(failed)}/{total}"
    return {"verdict": verdict, "reason": reason, "total": total, "failed": len(failed),
            "signature": next(iter(sigs)) if len(sigs) == 1 else None}


async def run_repeats(cfg: Config, work_dir, repeats: int, env_extra: dict | None = None,
                      timeout_s: int | None = None, on_progress=None) -> dict:
    """Runs the spec `repeats` times, one Playwright run each so progress shows live.

    Returns the verdict, per-run results, and the video and screenshots of a failing run.
    `on_progress(done, failed, total)` is called after each run.
    """
    base = _dir(work_dir)
    spec = os.path.normpath(os.path.join(base, "repro.spec.ts"))
    if os.path.dirname(spec) != base or not os.path.isfile(spec):
        return {"verdict": "error", "reason": "repro.spec.ts was not written", "total": 0, "failed": 0, "runs": []}
    results_dir = os.path.normpath(os.path.join(base, "test-results"))
    if os.path.dirname(results_dir) == base and os.path.isdir(results_dir):
        shutil.rmtree(results_dir)
    write_helper(base)
    spec_text = use_helper(spec)
    n = max(1, int(repeats))
    budget = float(timeout_s or cfg.repro_timeout_s)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + budget
    results: list[dict] = []
    first_error = ""
    for i in range(1, n + 1):
        left = deadline - loop.time()
        if left <= 0:
            first_error = first_error or "reproduction runs timed out"
            break
        got, error = await _run_once(cfg, base, i, env_extra or {}, left)
        results += got
        first_error = first_error or error
        if on_progress:
            on_progress(i, sum(1 for r in results if r["status"] in ("failed", "timedOut")), n)
    verdict = decide(results, n, spec_text)
    if not results and first_error:
        verdict["reason"] = first_error
    verdict["runs"] = [{"i": i + 1, "status": r["status"], "message": r["message"][:600],
                        "duration_ms": r["duration_ms"]} for i, r in enumerate(results)]
    verdict.update(pick_media(base, results))
    return verdict


async def _run_once(cfg: Config, base: str, index: int, env_extra: dict, timeout: float) -> tuple[list[dict], str]:
    report_path = os.path.normpath(os.path.join(base, f"report-{index}.json"))
    if os.path.dirname(report_path) != base:
        raise ValueError("bad report path")
    if os.path.isfile(report_path):
        os.remove(report_path)
    env = dict(os.environ)
    for k in SECRET_ENV:
        env.pop(k, None)
    env.update(env_extra)
    env["PLAYWRIGHT_JSON_OUTPUT_NAME"] = report_path
    env["NODE_PATH"] = str(cfg.node_root / "node_modules")
    env.setdefault("CI", "1")
    node = program("node")
    argv = checked_argv([node, rel_arg(cfg.playwright_cli, base), "test", "--config", "playwright.config.ts",
                         "--output", f"test-results/run-{index}", "--reporter", "json"])
    proc = await asyncio.create_subprocess_exec(*[shlex.quote(a) for a in argv], cwd=base, env=env,
                                                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                                                **procs.group_kwargs())
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        await procs.stop_tree(proc, grace_s=3)
        return [], "reproduction run timed out"
    procs.reap_group(proc.pid)
    report = None
    if os.path.isfile(report_path):
        with open(report_path, "r", encoding="utf-8") as fh:
            try:
                report = json.load(fh)
            except ValueError:
                report = None
    if report is None:
        try:
            report = json.loads(out.decode("utf-8", errors="replace"))
        except ValueError:
            tail = (err or out).decode("utf-8", errors="replace")[-600:]
            return [], f"no JSON report: {ANSI_RE.sub('', tail)}"
    results = collect_results(report)
    errors = report.get("errors") or []
    error = ANSI_RE.sub("", str(errors[0].get("message", "")))[:600] if errors and not results else ""
    return results, error


def pick_media(base: str, results: list[dict]) -> dict:
    """Copies the video and screenshots of the first failing run next to the spec (they survive re-runs)."""
    chosen = next((r for r in results if r["status"] in ("failed", "timedOut")), results[0] if results else None)
    media = {"video": None, "screenshots": []}
    if not chosen:
        return media
    shots = 0
    for a in chosen["attachments"]:
        src = os.path.normpath(os.path.abspath(a["path"] or ""))
        if not src.startswith(base + os.sep) or not os.path.isfile(src):
            continue
        if a["name"] == "video" and not media["video"]:
            dst = os.path.normpath(os.path.join(base, "bug-video" + (os.path.splitext(src)[1] or ".webm")))
            shutil.copyfile(src, dst)
            media["video"] = dst
        elif (a["type"] or "").startswith("image/") and shots < 4:
            shots += 1
            dst = os.path.normpath(os.path.join(base, f"bug-shot-{shots}{os.path.splitext(src)[1] or '.png'}"))
            shutil.copyfile(src, dst)
            media["screenshots"].append(dst)
    return media
