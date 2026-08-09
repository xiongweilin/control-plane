"""Stable run identity, PID file, single-instance guard and process-tree control.

Implements batch2 items 1 (stable run id), 4 (process-tree cancellation) and
5 (PID file + graceful shutdown + deterministic restart).
"""

from __future__ import annotations

import asyncio
import dataclasses
import os
import platform
import shutil
import socket
import subprocess
import sys
import time
import uuid
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class RunContext:
    run_id: str
    pid: int
    started_at: int
    hostname: str
    python_version: str
    pid_file: Path


_current_run: RunContext | None = None


def new_run_id() -> str:
    """Stable run identifier: timestamp + random suffix."""
    return f"run-{int(time.time())}-{uuid.uuid4().hex[:8]}"


def current_run() -> RunContext | None:
    return _current_run


def current_run_id() -> str:
    return _current_run.run_id if _current_run is not None else ""


def bootstrap(pid_file: Path) -> RunContext:
    """Create the run context for this process (idempotent within one process)."""
    global _current_run
    if _current_run is not None:
        return _current_run
    ctx = RunContext(
        run_id=new_run_id(),
        pid=os.getpid(),
        started_at=int(time.time()),
        hostname=socket.gethostname(),
        python_version=platform.python_version(),
        pid_file=pid_file,
    )
    _current_run = ctx
    return ctx


def write_pid_file(path: Path, pid: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(f"{pid}\n", encoding="ascii")
    os.replace(temporary, path)


def read_pid_file(path: Path) -> int | None:
    try:
        raw = path.read_text(encoding="ascii").strip()
    except OSError:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def remove_pid_file(path: Path) -> None:
    with suppress(FileNotFoundError):
        path.unlink()


def is_pid_alive(pid: int) -> bool:
    """Check whether a PID exists on this host (no psutil dependency)."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes

        process_query_limited_information = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not handle:
            return False
        try:
            exit_code = ctypes.c_ulong()
            ok = ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
            return bool(ok) and exit_code.value == 259  # STILL_ACTIVE
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def acquire_single_instance(pid_file: Path) -> tuple[bool, str]:
    """Deterministic single-instance guard.

    Returns (acquired, detail). When an old PID file points to a live process the
    guard refuses to start (double-instance protection, batch2 item 5). Stale
    PID files (dead process) are replaced.
    """
    existing = read_pid_file(pid_file)
    if existing is not None and existing != os.getpid() and is_pid_alive(existing):
        return False, f"another instance is running (pid {existing}, {pid_file})"
    write_pid_file(pid_file, os.getpid())
    return True, f"pid file written: {pid_file} (pid {os.getpid()})"


def _taskkill_command(pid: int) -> list[str]:
    return ["taskkill", "/PID", str(pid), "/T", "/F"]


def terminate_process_tree(pid: int, timeout: int = 15) -> None:
    """Terminate the whole process tree (Windows taskkill /T /F, POSIX fallback)."""
    if sys.platform == "win32":
        try:
            subprocess.run(  # noqa: S603 - pid comes from our own spawned subprocess
                _taskkill_command(pid),
                capture_output=True,
                timeout=timeout,
                shell=False,
                check=False,
            )
            return
        except (OSError, subprocess.TimeoutExpired):
            pass
    with suppress(ProcessLookupError):
        os.kill(pid, 9)  # noqa: S606 - POSIX fallback; Windows path uses taskkill above


async def terminate_process_tree_async(pid: int, timeout: int = 15) -> None:
    """Async variant of :func:`terminate_process_tree` for event-loop callers."""
    if sys.platform == "win32":
        try:
            proc = await asyncio.create_subprocess_exec(
                "taskkill",
                "/PID",
                str(pid),
                "/T",
                "/F",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.wait(), timeout=timeout)
            return
        except (OSError, TimeoutError):
            pass
    with suppress(ProcessLookupError):
        os.kill(pid, 9)  # noqa: S606 - POSIX fallback; Windows path uses taskkill above


def snapshot_processes() -> list[dict[str, Any]]:
    """Snapshot all processes as {pid, ppid, name} (single WMI query)."""
    if sys.platform != "win32":
        return []
    powershell = shutil.which("powershell.exe") or "powershell.exe"
    script = (
        "Get-CimInstance Win32_Process | ForEach-Object { "
        "\"$($_.ProcessId)`t$($_.ParentProcessId)`t$($_.Name)\" }"
    )
    try:
        proc = subprocess.run(  # noqa: S603 - fixed command line; WMI output parsed as data
            [powershell, "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            timeout=30,
            shell=False,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    rows: list[dict[str, Any]] = []
    for line in proc.stdout.decode("utf-8", errors="replace").splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        try:
            rows.append(
                {
                    "pid": int(parts[0]),
                    "ppid": int(parts[1]),
                    "name": str(parts[2]).lower(),
                }
            )
        except ValueError:
            continue
    return rows


def collect_descendants(pid: int, snapshot: list[dict[str, Any]]) -> list[int]:
    """Descendant PIDs of ``pid`` from a process snapshot (BFS over ParentProcessId)."""
    children: dict[int, list[int]] = {}
    for row in snapshot:
        children.setdefault(int(row["ppid"]), []).append(int(row["pid"]))
    found: list[int] = []
    queue = list(children.get(pid, []))
    seen = set(queue)
    while queue:
        current = queue.pop(0)
        found.append(current)
        for child in children.get(current, []):
            if child not in seen:
                seen.add(child)
                queue.append(child)
    return found


def assert_no_residual_processes(
    pid: int,
    names: tuple[str, ...] = ("git", "python", "pythonw", "node", "ssh"),
) -> tuple[bool, list[str]]:
    """Kill the process tree of ``pid`` and verify no named descendant survives.

    Returns (clean, residual_process_descriptions). Descendants are captured
    before the kill so detached/re-parented grandchildren are still detected.
    """
    snapshot = snapshot_processes()
    descendants = collect_descendants(pid, snapshot)
    terminate_process_tree(pid)
    time.sleep(0.2)  # allow taskkill to settle before re-checking
    after = snapshot_processes()
    alive = {int(row["pid"]): row for row in after}
    residual = [
        f"{alive[descendant]['name']} (pid {descendant})"
        for descendant in descendants
        if descendant in alive and alive[descendant]["name"] in names
    ]
    return (not residual), residual


def run_info_dict(run: RunContext | None = None) -> dict[str, str]:
    run = run or _current_run
    if run is None:
        return {"run_id": "", "pid": "", "started_at": "", "hostname": "", "python_version": ""}
    return {
        "run_id": run.run_id,
        "pid": str(run.pid),
        "started_at": str(run.started_at),
        "hostname": run.hostname,
        "python_version": run.python_version,
    }


def graceful_shutdown(pid_file: Path | None = None) -> None:
    """Best-effort shutdown cleanup: remove the PID file and record the stop."""
    global _current_run
    target = pid_file or (_current_run.pid_file if _current_run is not None else None)
    if target is not None:
        remove_pid_file(target)
    _current_run = None


def with_run_id(config: Any) -> Any:
    """Return a config copy carrying the current run id (used at startup)."""
    if _current_run is None:
        return config
    if config.run_id:
        return config
    return dataclasses.replace(config, run_id=_current_run.run_id)
