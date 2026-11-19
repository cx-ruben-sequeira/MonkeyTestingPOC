"""Tester: works one charter, generates inputs, judges expected vs actual, reports SUSPECT lines.

Each new suspect is stored at once and a Reproducer task is queued for it (highest priority),
so reproduction starts while the tester keeps going.
"""
from __future__ import annotations

from . import Agent, render


class Tester(Agent):
    kind = "test"

    def __init__(self, ctx):
        super().__init__(ctx)
        self.found = 0

    def prompt(self) -> str:
        c = self.task["charter"] or {}
        done = [t["title"] for t in self.store.tasks_with_status(self.sid, "done") if t["kind"] == "test"]
        return render("tester", **self.common_values(), area=c.get("area") or self.task["title"],
                      goal=c.get("goal") or "Explore and test.", start_url=c.get("start_url") or self.srow["url"],
                      focus=", ".join(c.get("focus") or []) or "general", map=self.map_summary(),
                      done=", ".join(done) or "(none)", suspects=self.suspects_summary())

    def on_marker(self, name: str, data) -> None:
        if name == "PAGE":
            self.record_page(data)
        elif name == "EDGE":
            self.record_edge(data)
        elif name == "SUSPECT" and isinstance(data, dict):
            self.add_suspect(data)

    def add_suspect(self, data: dict) -> int | None:
        url = str(data.get("url") or "")
        if url and not self.in_scope(url):
            return None
        data = dict(data, url=self.absolute(url) if url else self.srow["url"])
        sus = self.store.add_suspect(self.sid, self.tid, data)
        title = str(data.get("title") or "")[:120]
        if sus is None:
            self.store.add_event(self.sid, self.tid, f"tester", "info", f"duplicate suspect dropped: {title}")
            return None
        self.found += 1
        self.store.add_event(self.sid, self.tid, "tester", "suspect", f"suspect #{sus}: {title}")
        self.store.add_task(self.sid, "repro", f"Reproduce #{sus}: {title}", suspect_id=sus)
        return sus

    async def finish(self, result, stopped):
        return {"result": f"{self.found} suspects"}
