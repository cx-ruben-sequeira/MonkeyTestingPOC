"""mt (or `python -m mtlib`): serve | start <url> | status | cancel | delete | install | doctor.

The same commands on Windows, macOS and Linux. `mt install` replaces the old install.sh."""
from __future__ import annotations

import argparse
import asyncio
import logging
import re
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
        install_stop_handlers(asyncio.get_running_loop(), stop)
        await Dispatcher(cfg, store).run_forever(stop)

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass   # Windows: Ctrl+C can still land here before the handler is in place
    finally:
        srv.shutdown()
    return 0


def install_stop_handlers(loop: asyncio.AbstractEventLoop, stop: asyncio.Event) -> list[int]:
    """Ctrl+C / termination set `stop` so workers are shut down cleanly. loop.add_signal_handler only exists
    on POSIX; on Windows plain signal handlers (SIGINT, SIGTERM, SIGBREAK) hand over to the loop thread."""
    sigs = [signal.SIGINT, signal.SIGTERM] + ([signal.SIGBREAK] if hasattr(signal, "SIGBREAK") else [])
    done = []
    for sig in sigs:
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError, ValueError):
            try:
                signal.signal(sig, lambda *_: loop.call_soon_threadsafe(stop.set))
            except (ValueError, OSError):
                continue
        done.append(int(sig))
    return done


def cmd_start(cfg, args) -> int:
    store = _store(cfg)
    try:
        out = sessions.create(cfg, store, {
            "url": args.url, "mode": args.mode, "duration_min": args.minutes, "repeat_count": args.repeats,
            "max_actions": args.max_actions, "instructions": args.instructions or "",
            "allow_destructive": args.allow_destructive, "confirm_non_test": args.yes,
            "shots_every_action": args.screenshots, "page_load_s": args.page_load})
    except sessions.FormError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    print(f"session #{out['id']} queued" + (f" (warning: {out['warning']})" if out["warning"] else ""))
    print("It starts when `mt serve` is running.")
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


def parse_age(text: str) -> float:
    """'30d', '12h', '45m' or a plain number of days -> seconds."""
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([dhm]?)\s*", text or "")
    if not m:
        raise argparse.ArgumentTypeError("use a number of days, or e.g. 30d, 12h, 45m")
    return float(m.group(1)) * {"d": 86400, "h": 3600, "m": 60, "": 86400}[m.group(2)]


def human_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def cmd_delete(cfg, args) -> int:
    from . import deletion
    store = _store(cfg)
    if args.sessions:
        targets = [store.get_session(i) for i in args.sessions]
        missing = [i for i, t in zip(args.sessions, targets) if not t]
        if missing:
            print(f"no such session: {', '.join(map(str, missing))}", file=sys.stderr)
            return 1
    elif args.older_than is not None or args.status:
        targets = deletion.matching(store, args.older_than, args.status)
    else:
        print("say which sessions: mt delete 3 4, mt delete --older-than 30d, or mt delete --status finished",
              file=sys.stderr)
        return 2
    if not targets:
        print("nothing to delete")
        return 0
    total = 0
    for s in targets:
        p = deletion.preview(cfg, store, int(s["id"]))
        total += p["bytes"]
        run = " (running: it is cancelled first, then deleted by `mt serve`)" if p["running"] else ""
        print(f"#{s['id']:<4} {s['status']:<10} {s['url']}  {p['bugs']} bugs, {p['suspects']} suspects, "
              f"{p['events']} events, {human_bytes(p['bytes'])}{run}")
        if p["jira_keys"]:
            print(f"      Jira tickets stay in Jira: {', '.join(p['jira_keys'])}")
    print(f"{len(targets)} session(s), {human_bytes(total)} on disk. This cannot be undone.")
    if not args.yes:
        try:
            answer = input("Delete them? [y/N] ").strip().lower()
        except EOFError:
            answer = ""
        if answer not in ("y", "yes"):
            print("nothing deleted")
            return 1
    bad = 0
    for s in targets:
        out = deletion.request(cfg, store, int(s["id"]))
        if out["state"] == "incomplete":
            bad += 1
            print(f"#{s['id']}: delete incomplete, {len(out.get('left', []))} file(s) left; run it again")
        elif out["state"] == "cancelling":
            print(f"#{s['id']}: cancel requested; it is deleted once its workers stop (needs `mt serve`)")
        else:
            print(f"#{s['id']}: deleted ({human_bytes(out.get('bytes', 0))} freed)")
    return 1 if bad else 0


def cmd_install(cfg, args) -> int:
    from .setup_tools import install
    return install(cfg, with_deps=False if args.no_deps else None)


def cmd_doctor(cfg, args) -> int:
    from .setup_tools import doctor
    return doctor(cfg)


def safe_console() -> None:
    """Never crash printing a character the console's code page can't show (older Windows consoles)."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            continue


def main(argv=None) -> int:
    safe_console()
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
    st.add_argument("--page-load", type=int, default=120, metavar="SECONDS",
                    help="how long a page may take to load before it counts as broken (5-600, default 120)")
    st.add_argument("--screenshots", action="store_true", help="save a screenshot after every browser action")
    st.add_argument("--yes", action="store_true", help="start even if the host doesn't look like a test host")
    sub.add_parser("status", help="sessions and workers")
    c = sub.add_parser("cancel", help="cancel a session")
    c.add_argument("session", type=int)
    d = sub.add_parser("delete", help="delete sessions with all their data and files")
    d.add_argument("sessions", type=int, nargs="*", help="session numbers")
    d.add_argument("--older-than", type=parse_age, help="finished more than this long ago: 30d, 12h, 45m")
    d.add_argument("--status", choices=["finished", "done", "cancelled", "failed"],
                   help="only sessions with this status (finished = any session that is not running)")
    d.add_argument("--yes", action="store_true", help="don't ask for confirmation")
    i = sub.add_parser("install", help="install the pinned Playwright packages and Chromium, then run doctor")
    i.add_argument("--no-deps", action="store_true", help="Linux: skip Chromium's system libraries (they need sudo)")
    sub.add_parser("doctor", help="check tools, browsers, data folder and port; one line per check")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    cfg = config.load(config.Path(args.config) if args.config else None)
    return {"serve": cmd_serve, "start": cmd_start, "status": cmd_status, "cancel": cmd_cancel,
            "delete": cmd_delete, "install": cmd_install, "doctor": cmd_doctor}[args.cmd](cfg, args)
