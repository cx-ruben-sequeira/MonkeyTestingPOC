"""Reporter: runs once at the end of a session; marks duplicate bugs and writes the Markdown report.

The facts (counts, coverage, bug table) are built here from the database; the agent adds the
narrative (summary, coverage reading, recommendations). If the agent fails, the report still has
every fact section.
"""
from __future__ import annotations

import re
import time

from . import Agent, render, write_in

MARKER_LINE = re.compile(r"^\s*DUPLICATE:.*$", re.M)


def session_facts(store, sid: int) -> dict:
    s = store.get_session(sid)
    counts = store.session_counts(sid)
    sus = store.suspects(sid)
    bugs = [b for b in sus if b["status"] == "confirmed" and not b["duplicate_of"]]
    sev = {k: 0 for k in ("critical", "high", "medium", "low")}
    for b in bugs:
        sev[b["severity"]] = sev.get(b["severity"], 0) + 1
    tasks = store.tasks_for(sid)
    end = s["finished_at"] or time.time()
    minutes = (end - (s["started_at"] or end)) / 60
    return {"session": s, "counts": counts, "bugs": bugs, "severity": sev,
            "not_confirmed": [b for b in sus if b["status"] in ("not_confirmed", "error")],
            "pending": [b for b in sus if b["status"] in ("pending", "reproducing")],
            "tasks": tasks, "minutes": minutes, "pages": store.pages(sid)}


def facts_text(f: dict) -> str:
    s, c = f["session"], f["counts"]
    return "\n".join([
        f"- URL: {s['url']} | mode: {s['mode']} | repeats per suspect: {s['repeat_count']}",
        f"- Ran {f['minutes']:.1f} min of {s['duration_min']} | actions {s['actions_used']} of {s['max_actions']}",
        f"- Pages visited {c.get('pages_visited', 0)} of {c.get('pages_total', 0)} discovered",
        f"- Suspects {sum(1 for _ in f['bugs']) + len(f['not_confirmed']) + len(f['pending'])}: "
        f"{len(f['bugs'])} confirmed, {len(f['not_confirmed'])} not confirmed, {len(f['pending'])} not reproduced",
        f"- Workers run: " + ", ".join(f"{k} {sum(1 for t in f['tasks'] if t['kind'] == k)}"
                                       for k in ("explore", "test", "repro")),
        f"- Cost so far: ${s['cost_usd']:.2f}",
    ])


def bug_lines(rows: list[dict]) -> str:
    if not rows:
        return "(none)"
    return "\n".join(f"- #{b['id']} [{b['severity']}] {b['title']} @ {b['url']} — expected: {b['expected'][:200]}"
                     f" | actual: {b['actual'][:200]} | runs {b['runs_failed']}/{b['runs_total']} failed"
                     for b in rows)


def build_markdown(store, sid: int, narrative: str) -> str:
    f = session_facts(store, sid)
    s = f["session"]
    out = [f"# MonkeyTesting report — session #{sid}", "",
           f"**Target:** {s['url']}  ", f"**Mode:** {s['mode']} · **Repeats:** {s['repeat_count']} · "
           f"**Status:** {s['status']}", ""]
    out += ["## Facts", "", facts_text(f), "",
            "| Severity | Bugs |", "|---|---|"] + [f"| {k} | {v} |" for k, v in f["severity"].items()] + [""]
    if narrative.strip():
        out += [narrative.strip(), ""]
    out += ["## Confirmed bugs", ""]
    if not f["bugs"]:
        out.append("No bug failed in every reproduction run.")
    for b in f["bugs"]:
        out += [f"### #{b['id']} {b['title']}", "",
                f"- **Severity:** {b['severity']} · **Confidence:** "
                f"{'' if b['confidence'] is None else f'{b['confidence']:.0%}'} · **Page:** {b['url']}",
                f"- **Reproduced:** failed {b['runs_failed']}/{b['runs_total']} runs" +
                (f" · Jira {b['jira_key']}" if b["jira_key"] else ""), "", "**Steps to reproduce**", ""]
        out += [f"{i}. {st}" for i, st in enumerate(b["steps"] or [], 1)]
        out += ["", f"**Expected:** {b['expected']}", "", f"**Actual:** {b['actual']}", ""]
        if b["evidence"]:
            out += ["**Evidence**", ""] + [f"- `{e}`" for e in b["evidence"]] + [""]
        if b["video_path"]:
            out += [f"Video: `{b['video_path']}`", ""]
    if f["not_confirmed"]:
        out += ["## Not confirmed", ""]
        out += [f"- #{b['id']} {b['title']} — {b['runs_failed'] or 0}/{b['runs_total'] or 0} failed"
                f"{(': ' + b['reason']) if b['reason'] else ''}" for b in f["not_confirmed"]]
        out.append("")
    out += ["## Pages", ""] + [f"- {'✓' if p['visited'] else '○'} {p['url']}" for p in f["pages"]] + [""]
    return "\n".join(out)


class Reporter(Agent):
    kind = "report"
    uses_browser = False

    def prompt(self) -> str:
        f = session_facts(self.store, self.sid)
        return render("reporter", **self.common_values(), facts=facts_text(f), bugs=bug_lines(f["bugs"]),
                      not_confirmed=bug_lines(f["not_confirmed"]), map=self.map_summary(80))

    def on_marker(self, name: str, data) -> None:
        if name == "DUPLICATE" and isinstance(data, dict):
            try:
                bug, of = int(data["bug"]), int(data["of"])
            except (KeyError, TypeError, ValueError):
                return
            b, keep = self.store.get_suspect(bug), self.store.get_suspect(of)
            if b and keep and bug != of and b["session_id"] == self.sid == keep["session_id"]:
                self.store.update_suspect(bug, duplicate_of=of)
                self.store.add_event(self.sid, self.tid, "reporter", "info", f"bug #{bug} is a duplicate of #{of}")

    async def finish(self, result, stopped):
        narrative = MARKER_LINE.sub("", result.final_text if result.ok else "").strip()
        self.write_report(narrative)
        return {"result": "report written"}

    def on_failed(self, error: str) -> None:
        self.write_report("")

    def write_report(self, narrative: str) -> None:
        md = build_markdown(self.store, self.sid, narrative)
        path = write_in(self.ctx.session_dir, "report.md", md)
        summary = ""
        m = re.search(r"##\s*Summary\s*\n+(.+?)(\n##|\Z)", narrative, re.S)
        if m:
            summary = " ".join(m.group(1).split())[:1000]
        self.store.update_session(self.sid, report_path=str(path), summary=summary or None)
