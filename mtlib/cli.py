"""bin/mt: serve | start <url> | status | cancel <session>."""
from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
import threading
import time

from . import config, sessions
from .dispatcher import Dispatcher
from .server import make_server
from .store import Store


def _store(cfg) -> Store:
    cfg.ensure_dirs()
    return Store(cfg.db_path)


def cmd_serve(cfg, args) -> int:
    if cfg.host not in ("127.0.0.1", "localhost", "::1"):
        print("Refusing to serve on a non-loopback address; set [server] host = \"127.0.0.1\"", file=sys.stderr)
        return 2
    store = _store(cfg)
    if store.get_setting("max_workers") is None:
        store.set_setting("max_workers", str(cfg.max_workers))
    srv = make_server(cfg, store)
    threading.Thread(target=srv.serve_forever, name="http", daemon=True).start()
    print(f"MonkeyTesting dashboard: http://{cfg.host}:{cfg.port}  (Ctrl+C to stop)", flush=True)

    async def main():
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)
        await Dispatcher(cfg, store).run_forever(stop)

    try:
        asyncio.run(main())
    finally:
        srv.shutdown()
    return 0


def cmd_start(cfg, args) -> int:
    store = _store(cfg)
    try:
        out = sessions.create(cfg, store, {
            "url": args.url, "mode": args.mode, "duration_min": args.minutes, "repeat_count": args.repeats,
            "max_actions": args.max_actions, "instructions": args.instructions or "",
            "allow_destructive": args.allow_destructive, "confirm_non_test": args.yes})
    except sessions.FormError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    print(f"session #{out['id']} queued" + (f" (warning: {out['warning']})" if out["warning"] else ""))
    print("It starts when `bin/mt serve` is running.")
    return 0


def cmd_status(cfg, args) -> int:
    store = _store(cfg)
    rows = store.list_sessions(20)
    if not rows:
        print("no sessions yet")
    for s in rows:
        c = store.session_counts(int(s["id"]))
        left = ""
        if s["status"] == "running" and s["deadline"]:
            left = f", {max(0, (s['deadline'] - time.time()) / 60):.0f} min left"
        print(f"#{s['id']:<4} {s['status']:<10} {s['url']}  actions {s['actions_used']}/{s['max_actions']}, "
              f"bugs {c.get('confirmed', 0)}, suspects {sum(v for k, v in c.items() if not k.startswith('pages'))}"
              f"{left}")
    for t in store.running_tasks():
        print(f"   worker {t['worker_slot']}: s{t['session_id']} {t['kind']:<7} {t['title']} — {t['current_action'] or ''}")
    return 0


def cmd_cancel(cfg, args) -> int:
    store = _store(cfg)
    s = store.get_session(args.session)
    if not s:
        print("no such session", file=sys.stderr)
        return 1
    store.update_session(args.session, cancel_requested=1)
    print(f"cancel requested for session #{args.session}")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="mt", description="MonkeyTesting: autonomous exploratory QA")
    p.add_argument("--config", help="path to config.toml")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve", help="run the dashboard and the orchestrator")
    st = sub.add_parser("start", help="queue a test session")
    st.add_argument("url")
    st.add_argument("--mode", default="hybrid", choices=sessions.MODES)
    st.add_argument("--minutes", type=int, default=20)
    st.add_argument("--repeats", type=int, default=5)
    st.add_argument("--max-actions", type=int, default=300)
    st.add_argument("--instructions")
    st.add_argument("--allow-destructive", action="store_true")
    st.add_argument("--yes", action="store_true", help="start even if the host doesn't look like a test host")
    sub.add_parser("status", help="sessions and workers")
    c = sub.add_parser("cancel", help="cancel a session")
    c.add_argument("session", type=int)
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    cfg = config.load(config.Path(args.config) if args.config else None)
    return {"serve": cmd_serve, "start": cmd_start, "status": cmd_status, "cancel": cmd_cancel}[args.cmd](cfg, args)
