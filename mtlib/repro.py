"""Runs a suspect's repro.spec.ts N times with Playwright Test and reads the JSON report itself.

The confirmation rule lives here, not in any agent: a suspect is a Bug only when every one of the
N runs failed, and all failures are the same expect() assertion.
"""
from __future__ import annotations

import asyncio
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
  timeout: 60000,
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
    navigationTimeout: 20000,
  },
});
"""


def _dir(path) -> str:
    return os.path.normpath(os.path.abspath(os.fspath(path)))


def write_config(work_dir, browser: str, viewport: str) -> Path:
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
                    out.append({"status": r.get("status"), "message": ANSI_RE.sub("", msg or "")[:2000],
                                "duration_ms": r.get("duration"),
                                "attachments": [{"name": a.get("name"), "path": a.get("path"),
                                                 "type": a.get("contentType")}
                                                for a in r.get("attachments") or [] if a.get("path")]})
        for child in suite.get("suites") or []:
            walk(child)

    for s in report.get("suites") or []:
        walk(s)
    return out


def decide(results: list[dict], n: int) -> dict:
    """Applies the N-of-N rule to parsed results."""
    failed = [r for r in results if r["status"] in ("failed", "timedOut")]
    sigs = {signature(r["message"]) for r in failed}
    total = len(results)
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
    verdict = decide(results, n)
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
