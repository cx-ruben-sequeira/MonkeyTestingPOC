"""Jira Cloud REST v3: create an issue for a confirmed bug, then attach its video and screenshots.

The API token comes from the JIRA_API_TOKEN environment variable only; it is never stored.
"""
from __future__ import annotations

import base64
import json
import mimetypes
import os
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
        raise JiraError("Jira isn't configured: set [jira] base_url, email, project_key in config.toml "
                        "and the JIRA_API_TOKEN environment variable")
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
