"""Explorer: maps the app (pages, edges) and hands out test charters."""
from __future__ import annotations

from . import Agent, render


class Explorer(Agent):
    kind = "explore"

    def __init__(self, ctx):
        super().__init__(ctx)
        self.charters: list[dict] = []

    def prompt(self) -> str:
        return render("explorer", **self.common_values(), max_charters=self.cfg.max_charters)

    def on_marker(self, name: str, data) -> None:
        if name == "PAGE":
            self.record_page(data)
        elif name == "EDGE":
            self.record_edge(data)
        elif name == "CHARTER" and isinstance(data, dict) and len(self.charters) < self.cfg.max_charters:
            area = str(data.get("area") or "").strip()[:120]
            if area and area.lower() not in {c["area"].lower() for c in self.charters}:
                start = str(data.get("start_url") or self.srow["url"])
                self.charters.append({
                    "area": area, "goal": str(data.get("goal") or "")[:800],
                    "start_url": self.absolute(start) if self.in_scope(start) else self.srow["url"],
                    "focus": [str(f)[:120] for f in (data.get("focus") or [])][:8]})
                self.store.add_event(self.sid, self.tid, "explorer", "charter", f"charter: {area}")

    async def finish(self, result, stopped):
        if stopped in ("cancelled", "limit", "session_budget"):
            return {"result": f"stopped ({stopped}), {len(self.charters)} charters"}
        self.store.upsert_page(self.sid, self.srow["url"], visited=True)
        queue_charters(self, self.charters)
        return {"result": f"{len(self.store.pages(self.sid))} pages, {len(self.charters)} charters"}

    def on_failed(self, error: str) -> None:
        queue_charters(self, self.charters)


def queue_charters(agent: Agent, charters: list[dict]) -> None:
    """One Tester task per charter; a single whole-app charter when the Explorer found none."""
    store, row = agent.store, agent.store.get_session(agent.sid)
    if row["budget_hit_at"] or row["cancel_requested"]:
        return
    if not charters:
        charters = [{"area": "Whole application", "goal": "Explore and test the main flows and forms.",
                     "start_url": row["url"], "focus": ["forms", "navigation", "error handling"]}]
    remaining = max(0, int(row["max_actions"]) - int(row["actions_used"]))
    per = max(15, remaining // max(1, len(charters)))
    for c in charters:
        store.add_task(agent.sid, "test", c["area"], charter=c, action_budget=per)
