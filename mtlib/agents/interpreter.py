"""Interpreter: the first run of a session with instructions. It turns the free text into rules the
guard can enforce (focus pages, scope, excluded paths, forbidden actions) plus must-do items and notes.
No browser. If it fails, the rules come from rules.parse_fallback(). Either way the session's guard
file is rewritten with the rules before any browser worker starts."""
from __future__ import annotations

import asyncio

from .. import rules as rules_mod
from . import Agent, render


class Interpreter(Agent):
    kind = "rules"
    uses_browser = False

    def __init__(self, ctx):
        super().__init__(ctx)
        self.raw = None

    async def prepare(self) -> None:
        """Reads the Jira tickets named in the instructions (read-only, before the interpreter runs)."""
        from .. import jira
        text = self.srow["instructions"] or ""
        if not jira.ticket_keys(text):
            return
        tickets = await asyncio.to_thread(jira.read_tickets, self.cfg, text, bool(self.srow.get("ticket_comments")))
        self.store.update_session(self.sid, tickets=tickets)
        self.srow = self.store.get_session(self.sid)
        self.ctx.srow = self.srow
        for t in tickets:
            msg = (f"Jira {t['key']}: couldn't read it ({t['error']})" if t.get("error")
                   else f"Jira {t['key']} read: {t.get('summary', '')[:120]}")
            self.store.add_event(self.sid, self.tid, "interpreter", "warn" if t.get("error") else "info", msg)

    def prompt(self) -> str:
        return render("interpreter", **self.common_values(), origins=", ".join(self.allowed_origins()))

    def on_marker(self, name: str, data) -> None:
        if name == "RULES" and isinstance(data, dict):
            self.raw = data

    def save(self, rules: dict, how: str) -> dict:
        from ..sessions import write_guard
        self.store.update_session(self.sid, rules=rules)
        write_guard(self.cfg, self.store.get_session(self.sid))
        self.store.add_event(self.sid, self.tid, "interpreter", "rules", f"instructions understood ({how}): "
                             + (rules.get("summary") or rules_mod.as_text(rules).replace("\n", " ")))
        return {"result": rules.get("summary") or "rules saved"}

    async def finish(self, result, stopped):
        if self.raw is not None:
            rules = rules_mod.normalize(dict(self.raw, source="interpreter"), self.srow["url"], self.allowed_origins())
            return self.save(rules, "interpreter")
        self.on_failed("no RULES line in the answer")
        return {"result": "fallback rules (no RULES line)"}

    def on_failed(self, error: str) -> None:
        rules = rules_mod.parse_fallback(self.srow["instructions"] or "", self.srow["url"], self.allowed_origins())
        self.save(rules, f"fallback parser, because {error[:120]}")
