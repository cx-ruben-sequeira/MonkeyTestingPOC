"""Reproducer: writes repro.spec.ts for one suspect; the orchestrator then runs it N times itself."""
from __future__ import annotations

import os
from pathlib import Path

from .. import repro
from . import Agent, render


def read_secrets(session_dir) -> dict:
    """MT_USERNAME / MT_PASSWORD from the session's secrets.env (never stored in the database)."""
    base = os.path.normpath(os.path.abspath(os.fspath(session_dir)))
    path = os.path.normpath(os.path.join(base, "secrets.env"))
    out: dict = {}
    if os.path.dirname(path) != base or not os.path.isfile(path):
        return out
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            k, sep, v = line.rstrip("\n").partition("=")
            if sep and k in ("MT_USERNAME", "MT_PASSWORD"):
                out[k] = v
    return out


class Reproducer(Agent):
    kind = "repro"
    builtin_tools = "Read,Write,Edit,Bash"

    def __init__(self, ctx):
        super().__init__(ctx)
        self.suspect = self.store.get_suspect(int(self.task["suspect_id"]))
        self.spec_marker: dict = {}

    @property
    def work_dir(self) -> Path:
        return self.ctx.session_dir / f"suspect-{int(self.task['suspect_id'])}"

    @property
    def cwd(self) -> Path:
        return self.work_dir

    def allow(self) -> list[str]:
        w = os.path.normpath(os.path.abspath(os.fspath(self.work_dir)))
        return ["mcp__playwright", f"Read(/{w}/**)", f"Edit(/{w}/**)", f"Write(/{w}/**)",
                "Bash(npx playwright test:*)", "Bash(npx playwright test)"]

    def deny(self) -> list[str]:
        from . import BASE_DENY, BROWSER_DENY
        return BASE_DENY + BROWSER_DENY + ["Edit(/**/playwright.config.ts)", "Write(/**/playwright.config.ts)"]

    def prompt(self) -> str:
        self.work_dir.mkdir(parents=True, exist_ok=True)
        repro.write_config(self.work_dir, self.srow["browser"] or self.cfg.browser,
                           self.srow["viewport"] or self.cfg.viewport)
        s = self.suspect
        steps = "\n".join(f"{i}. {st}" for i, st in enumerate(s["steps"] or [], 1)) or "(not given)"
        self.store.update_suspect(s["id"], status="reproducing", runs_total=int(self.srow["repeat_count"]),
                                  runs_failed=0)
        return render("reproducer", **self.common_values(), suspect_id=s["id"], title=s["title"],
                      suspect_url=s["url"], steps=steps, expected=s["expected"] or "(not given)",
                      actual=s["actual"] or "(not given)", evidence="; ".join(s["evidence"] or []) or "(none)",
                      work_dir=str(self.work_dir), repeat_count=int(self.srow["repeat_count"]))

    def env(self) -> dict:
        return {**super().env(), **read_secrets(self.ctx.session_dir)}

    def on_marker(self, name: str, data) -> None:
        if name == "SPEC" and isinstance(data, dict):
            self.spec_marker = data

    async def finish(self, result, stopped):
        n = int(self.srow["repeat_count"])
        sid = int(self.suspect["id"])
        if stopped == "cancelled":
            self.store.update_suspect(sid, status="pending")
            return {}
        self.store.add_event(self.sid, self.tid, "reproducer", "info", f"suspect #{sid}: running the spec {n} times")

        def progress(done, failed, total):
            self.store.update_suspect(sid, runs_total=total, runs_failed=failed, reason=f"run {done}/{total}")
            self.store.update_task(self.tid, current_action=f"reproduction run {done}/{total}: {failed} failed")

        run = self.ctx.repro_fn or repro.run_repeats
        out = await run(self.cfg, self.work_dir, n, env_extra=read_secrets(self.ctx.session_dir),
                        on_progress=progress)
        verdict = out.get("verdict")
        status = {"confirmed": "confirmed", "not_confirmed": "not_confirmed"}.get(verdict, "error")
        reason = out.get("reason") or ""
        if self.spec_marker.get("status") == "passes" and self.spec_marker.get("reason"):
            reason += f" (reproducer: {self.spec_marker['reason']})"
        spec = os.path.normpath(os.path.join(os.fspath(self.work_dir), "repro.spec.ts"))
        self.store.update_suspect(
            sid, status=status, reason=reason[:600], runs_total=out.get("total") or n,
            runs_failed=out.get("failed") or 0, runs=out.get("runs") or [],
            spec_path=spec if os.path.isfile(spec) else None, video_path=out.get("video"),
            screenshots=out.get("screenshots") or [])
        label = {"confirmed": "BUG confirmed", "not_confirmed": "not confirmed"}.get(status, "reproduction error")
        self.store.add_event(self.sid, self.tid, "reproducer", "bug" if status == "confirmed" else "info",
                             f"suspect #{sid} {label}: {reason}")
        return {"result": f"{label} ({out.get('failed', 0)}/{out.get('total', 0)})"}

    def on_failed(self, error: str) -> None:
        self.store.update_suspect(int(self.suspect["id"]), status="error", reason=f"reproducer failed: {error}"[:600])
