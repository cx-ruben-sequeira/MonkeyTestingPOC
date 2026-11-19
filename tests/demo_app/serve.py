#!/usr/bin/env python3
"""Tiny demo shop for MonkeyTesting end-to-end runs, with planted bugs.

  python3 tests/demo_app/serve.py            # http://localhost:8099

Planted bugs
  1. /signup accepts an empty name and shows "Welcome, !" (should say "Name is required").
  2. /reports "Generate report" calls /api/report, which answers 500.
  3. The "Pricing" nav link goes to /pricing, which is a 404.
  4. Flaky: /search?q=apple shows "No results" about half the time (should always list Apple).
  5. /admin has a "Delete all data" button (the guard must block it).
"""
from __future__ import annotations

import html
import json
import random
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

PRODUCTS = ["Apple", "Banana", "Cherry", "Date", "Elderberry", "Fig", "Grape"]
NAV = ('<nav><a href="/">Home</a> | <a href="/signup">Sign up</a> | <a href="/search">Search</a> | '
       '<a href="/reports">Reports</a> | <a href="/pricing">Pricing</a> | <a href="/admin">Admin</a> | '
       '<a href="/about">About</a></nav><hr>')


def page(title: str, body: str) -> bytes:
    return (f"<!doctype html><html><head><meta charset='utf-8'><title>{title} - Demo Shop</title>"
            "<style>body{font-family:sans-serif;margin:24px;max-width:720px} .err{color:#a00} "
            ".ok{color:#070} input{margin:4px 0;padding:4px}</style></head>"
            f"<body>{NAV}<h1>{title}</h1>{body}</body></html>").encode()


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code: int, body: bytes, ctype: str = "text/html; charset=utf-8") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlsplit(self.path)
        q = parse_qs(u.query)
        if u.path == "/":
            items = "".join(f"<li>{p}</li>" for p in PRODUCTS)
            return self.send(200, page("Demo Shop", f"<p>Fresh fruit, delivered.</p><ul>{items}</ul>"))
        if u.path == "/signup":
            return self.send(200, page("Sign up", """
<form method="post" action="/signup">
  <label>Name <input name="name" id="name"></label><br>
  <label>Email <input name="email" id="email" type="email"></label><br>
  <button type="submit">Create account</button>
</form>"""))
        if u.path == "/search":
            term = (q.get("q") or [""])[0]
            hits = [p for p in PRODUCTS if term and term.lower() in p.lower()]
            if hits and random.random() < 0.5:  # planted flaky bug
                hits = []
            res = ""
            if term:
                res = ("<ul id='results'>" + "".join(f"<li>{p}</li>" for p in hits) + "</ul>") if hits \
                    else "<p id='results'>No results</p>"
            return self.send(200, page("Search", f"""
<form method="get" action="/search"><label>Search <input name="q" id="q" value="{html.escape(term)}"></label>
<button type="submit">Search</button></form>{res}"""))
        if u.path == "/reports":
            return self.send(200, page("Reports", """
<p>Monthly sales report.</p>
<button id="gen" onclick="gen()">Generate report</button>
<div id="out"></div>
<script>
async function gen() {
  const r = await fetch('/api/report');
  const out = document.getElementById('out');
  if (!r.ok) { console.error('report failed: ' + r.status); out.textContent = 'Something went wrong'; return; }
  const j = await r.json(); out.textContent = 'Report ready: ' + j.total + ' orders';
}
</script>"""))
        if u.path == "/api/report":  # planted bug: always 500
            return self.send(500, json.dumps({"error": "NullReferenceException"}).encode(), "application/json")
        if u.path == "/admin":
            return self.send(200, page("Admin", """
<p>Danger zone.</p>
<form method="post" action="/admin/delete-all"><button type="submit">Delete all data</button></form>"""))
        if u.path == "/about":
            return self.send(200, page("About", "<p>A demo app for MonkeyTesting.</p>"))
        return self.send(404, page("Not found", "<p>404 — this page doesn't exist.</p>"))

    def do_POST(self):
        u = urlsplit(self.path)
        n = int(self.headers.get("Content-Length") or 0)
        form = parse_qs(self.rfile.read(n).decode(), keep_blank_values=True)
        if u.path == "/signup":
            name = (form.get("name") or [""])[0]
            email = (form.get("email") or [""])[0]
            if email and "@" not in email:
                return self.send(200, page("Sign up", "<p class='err'>Please enter a valid email.</p>"))
            # planted bug: empty name is accepted
            return self.send(200, page("Sign up", f"<p class='ok'>Welcome, {html.escape(name)}!</p>"))
        if u.path == "/admin/delete-all":
            return self.send(200, page("Admin", "<p class='err'>All data deleted.</p>"))
        return self.send(404, page("Not found", ""))


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8099
    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    print(f"demo app on http://localhost:{port}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
