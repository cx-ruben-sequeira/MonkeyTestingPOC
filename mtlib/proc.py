"""Starting and stopping child process trees the same way on Windows, macOS and Linux.

A worker is a tree: claude -> node (Playwright MCP) -> chromium. Stopping only the top process
leaves the rest running, so every child is started in its own process group and the whole group
is stopped:
  POSIX:   start_new_session=True, then os.killpg(SIGTERM), then os.killpg(SIGKILL)
  Windows: CREATE_NEW_PROCESS_GROUP, then CTRL_BREAK_EVENT, then `taskkill /T /F /PID`
Programs are always started from an argument list (never a shell), found with shutil.which.
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import signal
import subprocess
import sys

log = logging.getLogger("mt.proc")
WINDOWS = os.name == "nt"


def group_kwargs() -> dict:
    """Keyword arguments for subprocess / asyncio.create_subprocess_exec that start a new process group."""
    if WINDOWS:
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def which(name: str) -> str | None:
    """Full path of a program on PATH (finds npx.cmd, claude.cmd and node.exe on Windows)."""
    return shutil.which(name)


def terminate_tree(pid: int) -> None:
    """Polite stop of the whole group (SIGTERM / CTRL_BREAK_EVENT)."""
    try:
        if WINDOWS:
            os.kill(pid, signal.CTRL_BREAK_EVENT)
        else:
            os.killpg(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError) as e:
        log.debug("terminate %s: %s", pid, e)


def kill_tree(pid: int) -> None:
    """Forced stop of the whole tree (SIGKILL to the group / taskkill /T /F)."""
    try:
        if WINDOWS:
            taskkill = shutil.which("taskkill") or "taskkill"
            subprocess.run([taskkill, "/T", "/F", "/PID", str(int(pid))], capture_output=True, timeout=30,
                           check=False)
        else:
            os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError, subprocess.SubprocessError) as e:
        log.debug("kill %s: %s", pid, e)


def reap_group(pid: int) -> None:
    """After the top process exited: stop children it left behind (node, chromium) in its group.
    POSIX only: there the group id stays valid while any member lives. On Windows a finished PID may
    already belong to an unrelated process, so nothing is killed by PID once the top process is gone."""
    if WINDOWS:
        return
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError) as e:
        log.debug("reap %s: %s", pid, e)


def kill_running(proc) -> None:
    """Forced stop of a tree whose top process may or may not still run (asyncio or subprocess.Popen)."""
    if proc.returncode is None:
        kill_tree(proc.pid)
    else:
        reap_group(proc.pid)


async def stop_tree(proc: asyncio.subprocess.Process, grace_s: float = 10.0) -> None:
    """Terminate, wait up to grace_s, then kill; finally stop children left behind in the group."""
    if proc.returncode is None:
        terminate_tree(proc.pid)
        try:
            await asyncio.wait_for(proc.wait(), timeout=grace_s)
        except asyncio.TimeoutError:
            log.info("process %s still running after %ss; killing it", proc.pid, grace_s)
    kill_running(proc)
    if proc.returncode is None:
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            log.warning("process %s did not exit after kill", proc.pid)
    reap_group(proc.pid)


def python() -> str:
    """The interpreter running MonkeyTesting, as an absolute path (hooks never rely on shebangs)."""
    return os.path.normpath(os.path.abspath(sys.executable))
