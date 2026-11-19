"""Where a session's workers may go, from the rules understood from the user's instructions.

Scope modes
  page   "This page only": the focus page(s) and anything that opens on them (dialogs, tabs, filters,
         query strings). Links that leave the page are recorded but not followed.
  below  "This page and below": the focus page(s) plus every route under their path.
  app    "Whole app": any route on the allowed origins.
Default: "below" when focus pages are given, "app" when not. Excluded paths are globs on the URL path
("/admin/**", "/settings/*") and always win. Login pages stay reachable (the session's login URL and
the usual auth routes such as /auth/..., /login, /oauth...), so a worker can always log in.
Stdlib only: hooks/guard.py imports this module too.
"""
from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit

MODES = ("page", "below", "app")
LABELS = {"page": "This page only", "below": "This page and below", "app": "Whole app"}
# Login routes, also hyphenated ones such as /login-organization (CxOne's tenant picker) or /sign-in-sso.
AUTH_PATH_RE = re.compile(r"^/(auth|login|logout|signin|sign-in|signout|sign-out|sso|oauth2?|openid|idp|saml|realms|"
                          r"account/login|users/sign_in|callback|select-tenant|tenant-select)([-_/.?]|$)", re.I)


def origin_of(url: str) -> str:
    try:
        p = urlsplit(url or "")
    except ValueError:
        return ""
    return f"{p.scheme}://{p.netloc}".lower() if p.scheme and p.netloc else ""


def path_of(url: str) -> str:
    try:
        return (urlsplit(url).path or "/").rstrip("/") or "/"
    except ValueError:
        return "/"


def excluded(path: str, patterns: list[str]) -> str | None:
    """The first exclude glob that matches the path ("/admin/**" also matches "/admin" itself)."""
    for pat in patterns or []:
        pat = str(pat).strip()
        if not pat:
            continue
        if not pat.startswith("/"):
            pat = "/" + pat
        if fnmatch.fnmatchcase(path, pat) or fnmatch.fnmatchcase(path, pat.rstrip("/") + "/**"):
            return pat
        base = pat[:-3] if pat.endswith("/**") else pat[:-2] if pat.endswith("/*") else pat
        if path == (base.rstrip("/") or "/") or (not any(c in base for c in "*?[") and path.startswith(base.rstrip("/") + "/")):
            return pat
    return None


@dataclass
class Scope:
    app_url: str
    origins: list[str]
    mode: str = "app"
    focus: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)
    login_url: str = ""

    @classmethod
    def for_session(cls, row: dict) -> "Scope":
        rules = row.get("rules") or {}
        origins = [o for o in [row.get("origin") or origin_of(row.get("url") or "")]
                   + list(row.get("allowed_origins") or []) if o]
        return cls.from_rules({"origins": origins, "scope": {
            "app_url": row.get("url") or "", "mode": rules.get("scope"), "focus": rules.get("focus_urls") or [],
            "exclude": rules.get("exclude_paths") or [], "login_url": row.get("login_url") or ""}})

    @classmethod
    def from_rules(cls, rules: dict) -> "Scope":
        sc = rules.get("scope") or {}
        focus = [str(u) for u in sc.get("focus") or [] if u]
        mode = sc.get("mode") or ("below" if focus else "app")
        return cls(app_url=sc.get("app_url") or "", origins=[str(o).lower().rstrip("/") for o in rules.get("origins") or []],
                   mode=mode if mode in MODES else "app", focus=focus,
                   exclude=[str(x) for x in sc.get("exclude") or []], login_url=sc.get("login_url") or "")

    def guard_rules(self) -> dict:
        return {"app_url": self.app_url, "mode": self.mode, "focus": self.focus, "exclude": self.exclude,
                "login_url": self.login_url}

    @property
    def label(self) -> str:
        return LABELS.get(self.mode, self.mode)

    @property
    def start_url(self) -> str:
        return self.absolute(self.focus[0]) if self.focus else self.app_url

    def absolute(self, url: str) -> str:
        return urljoin(self.app_url or "", url or "")

    def is_auth(self, url: str) -> bool:
        full = self.absolute(url)
        path = path_of(full)
        if self.login_url and origin_of(self.login_url) == origin_of(full) and \
                (path == path_of(self.login_url) or path.startswith(path_of(self.login_url).split("/protocol/")[0])):
            return True
        return bool(AUTH_PATH_RE.match(path))

    def reason(self, url: str) -> str | None:
        """Why the URL is out of scope, or None when a worker may go there."""
        full = self.absolute(url)
        o = origin_of(full)
        if not o:
            return None   # about:blank, data: ... are not navigation targets we police
        if self.origins and o not in self.origins:
            return f"{o} is not an allowed origin ({', '.join(self.origins)})"
        if self.is_auth(full):
            return None
        path = path_of(full)
        pat = excluded(path, self.exclude)
        if pat:
            return f"{path} is excluded by your instructions ({pat})"
        if self.mode == "app" or not self.focus:
            return None
        starts = [path_of(self.absolute(s)) for s in self.focus if origin_of(self.absolute(s)) in ("", o)]
        if self.mode == "page":
            if path in starts:
                return None
            return f"{path} is outside the page your instructions focus on ({', '.join(starts) or '-'})"
        for base in starts:
            if base == "/" or path == base or path.startswith(base + "/"):
                return None
        return f"{path} is outside the area your instructions focus on ({', '.join(b + '/**' for b in starts) or '-'})"

    def allows(self, url: str) -> bool:
        return self.reason(url) is None

    def describe(self) -> str:
        if self.mode == "app" and not self.exclude:
            return "Scope: the whole app on the allowed origins."
        parts = [f"Scope: {self.label}."]
        if self.focus:
            parts.append("Focus page(s): " + ", ".join(self.absolute(f) for f in self.focus) + ".")
        if self.exclude:
            parts.append("Never visit paths matching: " + ", ".join(self.exclude) + ".")
        if self.mode == "page":
            parts.append("Stay on the focus page: open its dialogs, tabs and filters, but do not follow links that "
                         "leave it (list them in PAGE.links instead).")
        elif self.mode == "below":
            parts.append("Only test routes under the focus page's path. Record other links in PAGE.links but never "
                         "open them.")
        return " ".join(parts)
