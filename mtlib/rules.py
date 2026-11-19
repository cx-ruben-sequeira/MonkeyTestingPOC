"""The user's "Instructions and constraints", turned into rules that code can enforce.

A short interpreter run (agents/interpreter.py) reads the free text and emits one RULES marker; this
module checks and normalizes it. If that run fails, parse_fallback() reads the obvious parts itself
(URLs to focus on, URLs/paths to stay away from). Either way the original text still goes to every
worker as binding instructions; the rules are what the guard hook enforces:
  focus_urls + scope   -> navigation outside the focus area is blocked, landing outside sends the worker back
  exclude_paths        -> navigation to those paths is blocked
  forbidden_actions    -> clicks/typing on elements whose label contains these words are blocked
  required, notes      -> not machine-checkable; repeated to every worker as must-do / must-not items
"""
from __future__ import annotations

import re
from urllib.parse import urljoin, urlsplit

from .scope import MODES, origin_of

URL_RE = re.compile(r"https?://[^\s<>\"'`)\]]+|(?<![\w.])/[A-Za-z0-9_\-./~%#?=&]*[A-Za-z0-9_\-/]")
NEGATION_RE = re.compile(r"\b(don'?t|do not|never|avoid|except|exclude|excluding|skip|not|without|outside of|"
                         r"stay away from|nunca|não|evit\w*|excep\w*)\b", re.I)
PAGE_ONLY_RE = re.compile(r"\b(only (this|that|the) page|this page only|that page only|only on (this|that) page|"
                          r"same page|single page)\b", re.I)
SCOPE_HINT = {"page": "only the focus page itself (its dialogs, tabs and filters)",
              "below": "the focus page and every route under its path", "app": "the whole app"}


def empty() -> dict:
    return {"focus_urls": [], "scope": "app", "exclude_paths": [], "forbidden_actions": [], "required": [],
            "notes": [], "summary": "", "source": "none"}


def _strings(v, n: int, maxlen: int) -> list[str]:
    if isinstance(v, str):
        v = [v]
    out = []
    for x in v or []:
        x = " ".join(str(x).split())[:maxlen]
        if x and x not in out:
            out.append(x)
    return out[:n]


def normalize(raw, app_url: str, origins: list[str]) -> dict:
    """Checked rules: focus URLs absolute and on an allowed origin, a known scope, paths starting with /."""
    out = empty()
    if not isinstance(raw, dict):
        return out
    allowed = [o.lower().rstrip("/") for o in origins if o]
    for u in _strings(raw.get("focus_urls") or raw.get("focus"), 10, 500):
        full = urljoin(app_url or "", u)
        p = urlsplit(full)
        if p.scheme in ("http", "https") and origin_of(full) in allowed and full not in out["focus_urls"]:
            out["focus_urls"].append(p._replace(fragment="").geturl())
    scope = str(raw.get("scope") or "").lower()
    out["scope"] = scope if scope in MODES else ("below" if out["focus_urls"] else "app")
    if out["scope"] != "app" and not out["focus_urls"]:
        out["scope"] = "app"
    for pth in _strings(raw.get("exclude_paths") or raw.get("exclude"), 30, 200):
        if pth.startswith("http"):
            if origin_of(pth) not in allowed:
                continue
            pth = urlsplit(pth).path or "/"
        if not pth.startswith("/"):
            pth = "/" + pth
        if pth not in out["exclude_paths"]:
            out["exclude_paths"].append(pth)
    out["forbidden_actions"] = [w.lower() for w in _strings(raw.get("forbidden_actions"), 20, 60) if len(w) >= 3]
    out["required"] = _strings(raw.get("required"), 20, 300)
    out["notes"] = _strings(raw.get("notes"), 20, 300)
    out["summary"] = " ".join(str(raw.get("summary") or "").split())[:400]
    out["source"] = str(raw.get("source") or "interpreter")[:20]
    return out


def parse_fallback(text: str, app_url: str, origins: list[str]) -> dict:
    """What can be read from the text without a model: focus URLs and excluded URLs/paths, per sentence."""
    focus, exclude = [], []
    for sentence in re.split(r"(?<=[.!?;\n])\s+", text or ""):
        for m in URL_RE.finditer(sentence):
            u = m.group(0).rstrip(".,;:!?")
            before = sentence[:m.start()]
            (exclude if NEGATION_RE.search(before[-60:]) else focus).append(u)
    scope = "page" if PAGE_ONLY_RE.search(text or "") else None
    rules = normalize({"focus_urls": focus, "exclude_paths": exclude, "scope": scope, "source": "parser",
                       "summary": "read from the instructions without the interpreter"}, app_url, origins)
    return rules


def has_content(rules: dict) -> bool:
    return bool(rules and (rules.get("focus_urls") or rules.get("exclude_paths") or rules.get("forbidden_actions")
                           or rules.get("required") or rules.get("notes")))


def as_text(rules: dict) -> str:
    """The understood rules, for every worker's prompt."""
    if not has_content(rules):
        return "(no extra rules)"
    lines = []
    if rules.get("focus_urls"):
        lines.append("- Focus ONLY on: " + ", ".join(rules["focus_urls"])
                     + f" ({SCOPE_HINT.get(rules.get('scope'), '')}). Everything else is out of scope.")
    if rules.get("exclude_paths"):
        lines.append("- Never visit: " + ", ".join(rules["exclude_paths"]))
    if rules.get("forbidden_actions"):
        lines.append("- Never click or type into anything labelled: " + ", ".join(rules["forbidden_actions"]))
    for r in rules.get("required") or []:
        lines.append(f"- MUST: {r}")
    for n in rules.get("notes") or []:
        lines.append(f"- {n}")
    return "\n".join(lines)
