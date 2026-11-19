"""Orchestrator: one pool of worker slots shared by every running session.

Per session: Explorer -> one Tester per charter -> one Reproducer per suspect -> Reporter.
Priority in the queue: repro > explore > test > report (store.PRIORITY). Session limits (deadline,
max actions) are enforced here, not by the agents: at the limit testers are stopped, queued
testers are dropped, reproducers still finish, then the Reporter runs.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import time
from collections import Counter
from pathlib import Path

from . import agents
from .config import Config
from .store import Store
from .worker import ClaudeRun, Event, Masker, RunResult, short, worker_env

log = logging.getLogger("mt.dispatcher")

LIMITED_KINDS = ("explore", "test")
LOOP_VERBS = ("click", "type", "fill_form", "select_option", "press_key", "navigate", "hover", "drag")


def session_dir(cfg: Config, sid: int) -> Path:
    base = os.path.normpath(os.path.abspath(os.fspath(cfg.runs_dir)))
    path = os.path.normpath(os.path.join(base, f"s{int(sid)}"))
    if os.path.dirname(path) != base:
        raise ValueError("bad session dir")
    os.makedirs(path, exist_ok=True)
    return Path(path)


class TaskExecution:
    """Runs one task: agent files -> claude -p -> live events -> agent.finish -> task row."""

    def __init__(self, disp: "Dispatcher", task: dict, slot: int):
        self.disp, self.cfg, self.store = disp, disp.cfg, disp.store
        self.task, self.slot = task, slot
        self.sid, self.tid = int(task["session_id"]), int(task["id"])
        self.run: ClaudeRun | None = None
        self.stop_reason: str | None = None
        self.actions = int(task["actions"] or 0)
        self.loops: Counter = Counter()
        self.soft_stopped = False
        self.last_input = ""
        self.last_flush = 0.0
        self.agent: agents.Agent | None = None

    @property
    def kind(self) -> str:
        return self.task["kind"]

    HARD_STOPS = ("cancelled", "shutdown")
    STOP_TEXT = {"task_budget": "this worker's action budget is used up.",
                 "session_budget": "the session's action budget is used up.",
                 "limit": "the session's time or action limit was reached.",
                 "loop": "the same action was repeated too many times on this page."}

    def stop(self, reason: str) -> None:
        """Hard stop (cancel, shutdown) kills the process now. Any other stop is soft: the guard hook
        refuses further browser actions so the agent reports what it found, and the process is
        killed only if it is still running after stop_grace_s."""
        hard = reason in self.HARD_STOPS
        if self.stop_reason and not hard:
            return
        self.stop_reason = reason
        if hard:
            if self.run:
                self.run.stop(reason)
            return
        self.soft_stopped = True
        flag = os.path.normpath(os.path.join(os.fspath(session_dir(self.cfg, self.sid)), f"t{self.tid}", "stop.flag"))
        os.makedirs(os.path.dirname(flag), exist_ok=True)
        with open(flag, "w", encoding="utf-8") as fh:
            fh.write(self.STOP_TEXT.get(reason, "a limit was reached."))
        self.event("info", f"limit ({reason}): browser refused from now on; {self.cfg.stop_grace_s}s to report")
        try:
            asyncio.get_running_loop().call_later(self.cfg.stop_grace_s, self._hard_stop, reason)
        except RuntimeError:
            pass

    def _hard_stop(self, reason: str) -> None:
        if self.run and self.run.proc and self.run.proc.returncode is None:
            self.event("warn", "still running after the grace period; stopping the process")
            self.run.stop(reason)

    def event(self, kind: str, text: str) -> None:
        self.store.add_event(self.sid, self.tid, f"{self.kind}-{self.slot}", kind, text)

    async def execute(self) -> None:
        try:
            await self._execute()
        except Exception as e:  # one task must never take the dispatcher down
            log.exception("task %s crashed", self.tid)
            self.store.update_task(self.tid, status="failed", error=short(f"Orchestrator error: {e}", 600),
                                   finished_at=time.time(), worker_slot=None, current_action=None)
            self.event("error", f"orchestrator error: {e}")
            if self.agent:
                try:
                    self.agent.on_failed(str(e))
                except Exception:
                    log.exception("on_failed crashed")

    async def _execute(self) -> None:
        srow = self.store.get_session(self.sid)
        sdir = session_dir(self.cfg, self.sid)
        tdir = sdir / f"t{self.tid}"
        tdir.mkdir(parents=True, exist_ok=True)
        ctx = agents.Context(cfg=self.cfg, store=self.store, srow=srow, task=self.task, task_dir=tdir,
                             session_dir=sdir, repro_fn=self.disp.repro_fn)
        self.agent = agent = agents.make(ctx)
        self.store.update_task(self.tid, worker_slot=self.slot, step_name="Starting Claude", current_action="",
                               error=None)
        self.event("info", f"started: {self.task['title']}")
        argv, prompt = agent.write_files()
        masker = Masker(_secret_values(sdir))
        self.run = ClaudeRun(argv, prompt, agent.cwd, worker_env(agent.env()), tdir / "stream.jsonl",
                             self.on_event, masker)
        if self.stop_reason in self.HARD_STOPS:
            self.run.stop(self.stop_reason)
        self.store.update_task(self.tid, step_name="Running")
        result = await self.run.run()
        self._flush(force=True)
        if result.parser.result:
            r = result.parser.result
            cost = float(r.get("total_cost_usd") or 0)
            self.store.add_usage(self.sid, self.tid, cost, result.parser.input_tokens, result.parser.tokens_out)
            self.store.update_task(self.tid, cost_usd=cost, claude_session=result.parser.session_id)
        await self._finish(agent, result)

    async def _finish(self, agent: agents.Agent, result: RunResult) -> None:
        now = time.time()
        reset = dict(worker_slot=None, current_action=None, stop_reason=self.stop_reason)
        if self.stop_reason == "shutdown":
            self.store.update_task(self.tid, status="queued", started_at=None, **reset)
            self.event("info", "stopped because the orchestrator shut down; back in the queue")
            return
        if self.stop_reason == "cancelled":
            await agent.finish(result, "cancelled")
            self.store.update_task(self.tid, status="cancelled", finished_at=now, **reset)
            self.event("info", "cancelled")
            return
        if result.ok or self.stop_reason:
            self.store.update_task(self.tid, step_name="Wrapping up")
            fields = await agent.finish(result, self.stop_reason)
            self.store.update_task(self.tid, status="done", finished_at=time.time(), step_name="Finished",
                                   **reset, **fields)
            why = f" (stopped: {self.stop_reason})" if self.stop_reason else ""
            self.event("result", f"finished{why}: {fields.get('result', '')}")
            return
        attempts = int(self.store.get_task(self.tid)["attempts"] or 0)
        err = result.error
        if result.rate_limited:
            err = "Claude usage limit reached: " + err
        if attempts < self.cfg.max_attempts:
            # back to the queue; older ids go first within the same priority, so it is next in line
            self.store.update_task(self.tid, status="queued", started_at=None, error=err, **reset)
            self.event("warn", f"run failed (attempt {attempts}); retrying: {short(err, 200)}")
        else:
            self.store.update_task(self.tid, status="failed", finished_at=now, error=err, **reset)
            self.event("error", f"run failed: {short(err, 200)}")
            agent.on_failed(err)

    # ------------------------------------------------------------ live stream
    def on_event(self, ev: Event, run: ClaudeRun) -> None:
        agent = self.agent
        if ev.kind == "tool":
            if ev.browser and self.soft_stopped:
                pass  # refused by the guard hook; not an action
            elif ev.browser:
                self.actions += 1
                used = self.store.add_actions(self.sid, 1)
                self.store.add_event(self.sid, self.tid, f"{self.kind}-{self.slot}", "action", ev.text)
                self._check_limits(ev, run, used)
            else:
                self.event("tool", ev.text)
            self.task["current_action"] = ev.text
        elif ev.kind == "marker":
            if ev.name in ("ACTION", "NOTE"):
                self.event("note", ev.text)
            if agent:
                agent.on_marker(ev.name, ev.data)
        elif ev.kind == "page":
            if agent and self.kind in LIMITED_KINDS:
                agent.record_page({"url": ev.text, "visited": True})  # the browser was really there
        elif ev.kind == "blocked":
            self.event("blocked", ev.text)
        elif ev.kind == "tool_error":
            self.event("tool_error", ev.text)
        elif ev.kind == "warn":
            self.event("warn", ev.text)
        self._flush()

    def _check_limits(self, ev: Event, run: ClaudeRun, used: int) -> None:
        if self.kind not in LIMITED_KINDS:
            return
        srow = self.store.get_session(self.sid)
        budget = self.task["action_budget"]
        if budget and self.actions >= int(budget):
            self.event("info", f"action budget for this charter used ({self.actions})")
            self.stop("task_budget")
        elif used >= int(srow["max_actions"]):
            self.stop("session_budget")
        verb = ev.name.replace("mcp__playwright__browser_", "")
        if verb in ("type", "fill_form", "select_option"):
            self.last_input = ev.text + json.dumps(ev.data, sort_keys=True, default=str)[:400]
        if verb in LOOP_VERBS:
            # the same action on the same page after the same input: resubmitting a form with new values is fine
            key = (run.parser.url, ev.text, self.last_input)
            self.loops[key] += 1
            if self.loops[key] > self.cfg.loop_repeat_limit:
                self.event("warn", f"loop guard: '{short(ev.text, 80)}' repeated {self.loops[key]} times; stopping")
                self.stop("loop")

    def _flush(self, force: bool = False) -> None:
        now = time.time()
        if not force and now - self.last_flush < 0.5:
            return
        self.last_flush = now
        p = self.run.parser if self.run else None
        self.store.update_task(self.tid, actions=self.actions, current_action=self.task.get("current_action"),
                               tokens_out=p.tokens_out if p else 0)


def _secret_values(sdir: Path) -> list[str]:
    from .agents.reproducer import read_secrets
    return list(read_secrets(sdir).values())


class Dispatcher:
    def __init__(self, cfg: Config, store: Store, repro_fn=None, tick_s: float = 0.5):
        self.cfg, self.store = cfg, store
        self.repro_fn = repro_fn
        self.tick_s = tick_s
        self.running: dict[int, tuple[asyncio.Task, TaskExecution]] = {}

    @property
    def max_workers(self) -> int:
        try:
            return max(1, min(10, int(self.store.get_setting("max_workers", str(self.cfg.max_workers)))))
        except ValueError:
            return self.cfg.max_workers

    def free_slot(self) -> int:
        used = {ex.slot for _, ex in self.running.values()}
        return next(i for i in range(1, 100) if i not in used)

    async def run_forever(self, stop: asyncio.Event) -> None:
        n = self.store.recover()
        if n:
            self.store.add_event(None, None, "dispatcher", "info", f"{n} interrupted tasks back in the queue")
        while not stop.is_set():
            try:
                self.tick()
            except Exception:
                log.exception("tick failed")
            try:
                await asyncio.wait_for(stop.wait(), self.tick_s)
            except asyncio.TimeoutError:
                pass
        await self.shutdown()

    async def shutdown(self) -> None:
        for _, ex in list(self.running.values()):
            ex.stop("shutdown")
        if self.running:
            await asyncio.wait([t for t, _ in self.running.values()], timeout=20)

    async def drain(self, timeout: float = 60) -> None:
        """Test helper: tick until no session is active."""
        end = time.time() + timeout
        while time.time() < end:
            self.tick()
            if not self.store.list_sessions(statuses=("queued", "running", "reporting")) and not self.running:
                return
            await asyncio.sleep(self.tick_s)
        raise TimeoutError("sessions still active")

    # ------------------------------------------------------------ one tick
    def tick(self) -> None:
        for tid in [t for t, (task, _) in self.running.items() if task.done()]:
            self.running.pop(tid)
        for s in self.store.list_sessions(200, statuses=("queued", "running", "reporting")):
            self.advance(s)
        while len(self.running) < self.max_workers:
            task = self.store.claim_next()
            if not task:
                break
            ex = TaskExecution(self, task, self.free_slot())
            self.running[int(task["id"])] = (asyncio.get_running_loop().create_task(ex.execute()), ex)

    def _running_for(self, sid: int, kinds=None) -> list[TaskExecution]:
        return [ex for _, ex in self.running.values()
                if ex.sid == sid and (kinds is None or ex.kind in kinds)]

    def advance(self, s: dict) -> None:
        sid, now = int(s["id"]), time.time()
        if s["cancel_requested"]:
            for ex in self._running_for(sid):
                ex.stop("cancelled")
            self.store.cancel_queued(sid, None, "cancelled")
            if not self._running_for(sid):
                self.store.update_session(sid, status="cancelled", phase="cancelled", finished_at=now)
                self.store.add_event(sid, None, "dispatcher", "info", "session cancelled")
            return
        if s["status"] == "queued":
            seed = s["seed"] or random.randint(1, 10**6)
            self.store.update_session(sid, status="running", phase="exploring", started_at=now, seed=seed,
                                      deadline=now + int(s["duration_min"]) * 60)
            budget = max(10, int(int(s["max_actions"]) * self.cfg.explorer_action_share))
            self.store.add_task(sid, "explore", "Map the application", action_budget=budget)
            self.store.add_event(sid, None, "dispatcher", "info", f"session started on {s['url']} ({s['mode']})")
            return
        tasks = self.store.tasks_for(sid)
        active = [t for t in tasks if t["status"] in ("queued", "running")]
        if s["status"] == "running":
            over = (s["deadline"] and now >= s["deadline"]) or int(s["actions_used"]) >= int(s["max_actions"])
            if over and not s["budget_hit_at"]:
                why = "time is up" if s["deadline"] and now >= s["deadline"] else "action budget used"
                self.store.update_session(sid, budget_hit_at=now, phase="wrapping up")
                self.store.add_event(sid, None, "dispatcher", "info", f"{why}: stopping testers, finishing reproductions")
                for kind in LIMITED_KINDS:
                    self.store.cancel_queued(sid, kind, "limit")
                for ex in self._running_for(sid, LIMITED_KINDS):
                    ex.stop("limit")
                return
            limited_active = [t for t in active if t["kind"] in LIMITED_KINDS]
            if not over and not limited_active:
                if any(t["kind"] == "explore" for t in tasks) and self._next_round(s):
                    return
                if not s["budget_hit_at"]:
                    self.store.update_session(sid, budget_hit_at=now, phase="wrapping up")
            if any(t["kind"] == "explore" and t["status"] in ("done", "failed") for t in tasks) \
                    and s["phase"] == "exploring":
                self.store.update_session(sid, phase="testing")
            if (s["budget_hit_at"] or over) and not active:
                self.store.update_session(sid, status="reporting", phase="reporting")
                self.store.add_task(sid, "report", "Write the session report")
                self.store.add_event(sid, None, "dispatcher", "info", "all workers done; writing the report")
            return
        if s["status"] == "reporting" and not active:
            self.store.update_session(sid, status="done", phase="done", finished_at=now)
            self.store.add_event(sid, None, "dispatcher", "result", "session finished")

    def _next_round(self, s: dict) -> bool:
        """Budget left and pages discovered but not visited: a few more testers on them."""
        sid = int(s["id"])
        if int(s["rounds"]) >= self.cfg.max_rounds:
            return False
        tried = {(t["charter"] or {}).get("start_url") for t in self.store.tasks_for(sid) if t["kind"] == "test"}
        todo = [p for p in self.store.pages(sid) if not p["visited"] and p["url"] not in tried][:3]
        remaining = int(s["max_actions"]) - int(s["actions_used"])
        if not todo or remaining < 10:
            return False
        per = max(10, remaining // len(todo))
        for p in todo:
            self.store.add_task(sid, "test", f"Unvisited page {p['url']}", action_budget=per, charter={
                "area": f"Unvisited page {p['url']}", "start_url": p["url"],
                "goal": "This page was discovered but nobody tested it yet. Test it thoroughly.",
                "focus": ["forms", "links", "error handling"]})
        self.store.update_session(sid, rounds=int(s["rounds"]) + 1)
        self.store.add_event(sid, None, "dispatcher", "info", f"round {int(s['rounds']) + 1}: {len(todo)} unvisited pages")
        return True
