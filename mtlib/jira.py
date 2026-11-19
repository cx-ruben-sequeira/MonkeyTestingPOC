"""Jira Cloud REST v3: create an issue for a confirmed bug, then attach its video and screenshots.

The API token is entered in the dashboard (kept in memory only) or comes from JIRA_API_TOKEN; it is
never stored in the database or logged. Tickets named in a session's instructions are read here, read-only.
"""
from __future__ import annotations

import base64
import json
import mimetypes
import os
import re
import uuid
from urllib import error, request
from urllib.parse import urlsplit

from .config import Config


class JiraError(RuntimeError):
    pass


def _auth(cfg: Config) -> str:
    raw = f"{cfg.jira_email}:{cfg.jira_token}".encode()
    return "Basic " + base64.b64encode(raw).decode()


def _check_base(cfg: Config) -> str:
    base = cfg.jira_base_url.rstrip("/")
    p = urlsplit(base)
    if p.scheme != "https" and p.hostname not in ("127.0.0.1", "localhost"):
        raise JiraError("Jira base_url must be https")
    return base


def _call(cfg: Config, method: str, path: str, body: bytes | None, headers: dict) -> dict:
    url = _check_base(cfg) + path
    req = request.Request(url, data=body, method=method, headers={"Authorization": _auth(cfg),
                                                                  "Accept": "application/json", **headers})
    try:
        with request.urlopen(req, timeout=60) as resp:
            status = resp.status
            raw = resp.read()
    except error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:500]
        raise JiraError(f"Jira answered {e.code}: {detail}") from None
    except error.URLError as e:
        raise JiraError(f"Couldn't reach Jira: {e.reason}") from None
    if not 200 <= status < 300:
        raise JiraError(f"Jira answered {status}")
    try:
        return json.loads(raw.decode("utf-8")) if raw else {}
    except ValueError:
        return {}


def _p(text: str) -> dict:
    return {"type": "paragraph", "content": [{"type": "text", "text": text}] if text else []}


def _h(text: str) -> dict:
    return {"type": "heading", "attrs": {"level": 3}, "content": [{"type": "text", "text": text}]}


def description(bug: dict, session: dict) -> dict:
    """Atlassian Document Format body: steps, expected, actual, severity, URL, session, evidence."""
    steps = [{"type": "listItem", "content": [_p(str(s))]} for s in bug.get("steps") or []]
    content = [_p(f"Found by MonkeyTesting session #{session['id']} on {session['url']} "
                  f"({session['mode']} mode). Reproduced in {bug.get('runs_failed')}/{bug.get('runs_total')} runs."),
               _h("Steps to reproduce")]
    content.append({"type": "orderedList", "content": steps} if steps else _p("(not recorded)"))
    content += [_h("Expected result"), _p(bug.get("expected") or ""),
                _h("Actual result"), _p(bug.get("actual") or ""),
                _h("Details"),
                _p(f"Severity: {bug.get('severity')} · Confidence: {bug.get('confidence')} · Page: {bug.get('url')}")]
    if bug.get("evidence"):
        content += [_h("Evidence"), {"type": "codeBlock", "content": [
            {"type": "text", "text": "\n".join(str(e) for e in bug["evidence"])[:20000]}]}]
    return {"type": "doc", "version": 1, "content": content}


SEVERITY_PRIORITY = {"critical": "Highest", "high": "High", "medium": "Medium", "low": "Low"}


def create_issue(cfg: Config, bug: dict, session: dict) -> dict:
    if not cfg.jira_ready:
        raise JiraError("Jira isn't set up: open the Jira connection panel in the dashboard (URL, email, "
                        "project key and API token)")
    fields = {"project": {"key": cfg.jira_project_key}, "issuetype": {"name": cfg.jira_issue_type},
              "summary": f"[MonkeyTesting] {bug['title']}"[:250], "description": description(bug, session),
              "labels": [l.replace(" ", "-") for l in cfg.jira_labels]}
    body = json.dumps({"fields": fields}).encode()
    out = _call(cfg, "POST", "/rest/api/3/issue", body, {"Content-Type": "application/json"})
    key = out.get("key")
    if not key:
        raise JiraError("Jira didn't return an issue key")
    return {"key": key, "url": f"{_check_base(cfg)}/browse/{key}"}


def attach(cfg: Config, key: str, paths: list[str], allowed_root: str) -> list[str]:
    """Uploads each file (must be under allowed_root). Returns the names that were attached."""
    root = os.path.normpath(os.path.abspath(allowed_root))
    done = []
    for raw in paths:
        path = os.path.normpath(os.path.abspath(raw))
        if not path.startswith(root + os.sep) or not os.path.isfile(path):
            continue
        name = os.path.basename(path)
        with open(path, "rb") as fh:
            data = fh.read()
        boundary = uuid.uuid4().hex
        ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
        body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{name}\"\r\n"
                f"Content-Type: {ctype}\r\n\r\n").encode() + data + f"\r\n--{boundary}--\r\n".encode()
        _call(cfg, "POST", f"/rest/api/3/issue/{key}/attachments", body,
              {"Content-Type": f"multipart/form-data; boundary={boundary}", "X-Atlassian-Token": "no-check"})
        done.append(name)
    return done


# ---------------------------------------------------------------- reading tickets (read-only)
TICKET_KEY_RE = re.compile(r"(?<![A-Za-z0-9_-])([A-Z][A-Z0-9_]{1,19}-[1-9]\d{0,6})(?![A-Za-z0-9_-])")
NOT_TICKETS = {"UTF", "ISO", "SHA", "TLS", "SSL", "HTTP", "CVE", "RFC", "AES", "MD", "GPT", "COVID", "WCAG", "IPV",
               "X", "A", "B", "V"}
MAX_TICKETS = 5
TICKET_TEXT_CHARS = 6000
EXTRA_FIELD_RE = re.compile(r"acceptance|criteria|steps|scenario|expected|test (notes|plan|cases)|how to test|"
                            r"reproduc", re.I)


def ticket_keys(text: str) -> list[str]:
    """Jira keys named in free text: AST-1234, or links like .../browse/AST-1234 or ?selectedIssue=AST-1234."""
    out = []
    for key in TICKET_KEY_RE.findall(text or ""):
        if key.split("-")[0] not in NOT_TICKETS and key not in out:
            out.append(key)
    return out[:MAX_TICKETS]


def adf_to_text(node, depth: int = 0) -> str:
    """Atlassian Document Format to plain text (paragraphs, lists, headings, code, tables, links)."""
    if node is None or depth > 40:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "".join(adf_to_text(n, depth + 1) for n in node)
    if not isinstance(node, dict):
        return ""
    kind = node.get("type")
    if kind == "text":
        return str(node.get("text") or "")
    if kind == "hardBreak":
        return "\n"
    if kind in ("mention", "emoji", "inlineCard"):
        attrs = node.get("attrs") or {}
        return str(attrs.get("text") or attrs.get("shortName") or attrs.get("url") or "")
    inner = adf_to_text(node.get("content") or [], depth + 1)
    if kind == "listItem":
        return "- " + inner.strip().replace("\n", "\n  ") + "\n"
    if kind in ("paragraph", "heading", "codeBlock", "blockquote", "tableRow", "panel", "rule"):
        return inner.strip() + "\n"
    if kind in ("tableCell", "tableHeader"):
        return inner.strip() + " | "
    return inner


def _field_text(value) -> str:
    if isinstance(value, dict) and value.get("type") == "doc":
        return adf_to_text(value).strip()
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return ", ".join(str((v or {}).get("value") or (v or {}).get("name") or v) if isinstance(v, dict) else str(v)
                         for v in value)
    if isinstance(value, dict):
        return str(value.get("value") or value.get("name") or "")
    return ""


def myself(cfg: Config) -> dict:
    """Who the token belongs to (to test the connection)."""
    out = _call(cfg, "GET", "/rest/api/3/myself", None, {})
    return {"name": out.get("displayName") or "", "email": out.get("emailAddress") or ""}


def get_issue(cfg: Config, key: str, comments: bool = False) -> dict:
    """One ticket as plain text: summary, type, status, description, acceptance-criteria-like fields and,
    when asked, the latest comments. Read-only."""
    if not TICKET_KEY_RE.fullmatch(key or ""):
        raise JiraError(f"not a Jira key: {key!r}")
    data = _call(cfg, "GET", f"/rest/api/3/issue/{key}?expand=names&fields=*all", None, {})
    fields, names = data.get("fields") or {}, data.get("names") or {}
    parts = []
    desc = _field_text(fields.get("description"))
    if desc:
        parts.append("Description:\n" + desc)
    for fid, fname in names.items():
        if fid.startswith("customfield_") and EXTRA_FIELD_RE.search(str(fname or "")):
            text = _field_text(fields.get(fid))
            if text:
                parts.append(f"{fname}:\n{text}")
    if comments:
        items = ((fields.get("comment") or {}).get("comments") or [])[-10:]
        lines = [f"- {((c.get('author') or {}).get('displayName') or 'someone')}: {adf_to_text(c.get('body')).strip()}"
                 for c in items if c.get("body")]
        if lines:
            parts.append("Comments (latest last):\n" + "\n".join(lines))
    text = "\n\n".join(parts)
    if len(text) > TICKET_TEXT_CHARS:
        text = text[:TICKET_TEXT_CHARS] + "\n[... cut: the ticket is longer]"
    return {"key": key, "summary": str(fields.get("summary") or ""),
            "type": str((fields.get("issuetype") or {}).get("name") or ""),
            "status": str((fields.get("status") or {}).get("name") or ""),
            "url": f"{_check_base(cfg)}/browse/{key}", "text": text}


def read_tickets(cfg: Config, text: str, comments: bool = False) -> list[dict]:
    """Every ticket named in the text; a ticket that can't be read comes back with an `error`."""
    out = []
    for key in ticket_keys(text):
        if not cfg.jira_read_ready:
            out.append({"key": key, "error": "Jira isn't connected: open the Jira connection panel in the dashboard"})
            continue
        try:
            out.append(get_issue(cfg, key, comments))
        except JiraError as e:
            out.append({"key": key, "error": str(e)[:300]})
    return out
