from __future__ import annotations

import os
import re
import subprocess
import sys
import time

import pytest

from control_plane.runtime import (
    RunContext,
    acquire_single_instance,
    assert_no_residual_processes,
    bootstrap,
    collect_descendants,
    graceful_shutdown,
    is_pid_alive,
    new_run_id,
    read_pid_file,
    remove_pid_file,
    run_info_dict,
    snapshot_processes,
    terminate_process_tree,
    write_pid_file,
)


def test_new_run_id_format() -> None:
    run_id = new_run_id()
    assert re.fullmatch(r"run-\d{10}-[0-9a-f]{8}", run_id)
    assert new_run_id() != run_id


def test_pid_file_roundtrip(tmp_path) -> None:
    path = tmp_path / "data" / "control-plane.pid"
    write_pid_file(path, 4242)
    assert read_pid_file(path) == 4242
    remove_pid_file(path)
    assert read_pid_file(path) is None
    remove_pid_file(path)  # idempotent


def test_is_pid_alive_current_process() -> None:
    assert is_pid_alive(os.getpid()) is True
    assert is_pid_alive(0) is False
    assert is_pid_alive(-5) is False


def test_is_pid_alive_dead_process() -> None:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait(timeout=30)
    time.sleep(0.2)
    assert is_pid_alive(proc.pid) is False


def test_acquire_single_instance_rejects_live_pid(tmp_path) -> None:
    path = tmp_path / "cp.pid"
    live = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        time.sleep(0.5)
        write_pid_file(path, live.pid)
        acquired, detail = acquire_single_instance(path)
        assert acquired is False
        assert "another instance" in detail
    finally:
        live.kill()
        live.wait(timeout=10)


def test_acquire_single_instance_replaces_stale_pid(tmp_path) -> None:
    path = tmp_path / "cp.pid"
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait(timeout=30)
    time.sleep(0.2)
    write_pid_file(path, dead.pid)
    acquired, detail = acquire_single_instance(path)
    assert acquired is True
    assert read_pid_file(path) == os.getpid()
    assert "pid file written" in detail
    remove_pid_file(path)


def test_bootstrap_and_run_info(tmp_path) -> None:
    run = bootstrap(tmp_path / "cp.pid")
    assert isinstance(run, RunContext)
    assert run.pid == os.getpid()
    info = run_info_dict(run)
    assert info["run_id"] == run.run_id
    assert info["pid"] == str(run.pid)
    graceful_shutdown(run.pid_file)
    assert run_info_dict()["run_id"] == ""


def test_terminate_process_tree_kills_children() -> None:
    script = (
        "import subprocess, sys, time; "
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
        "time.sleep(60)"
    )
    proc = subprocess.Popen([sys.executable, "-c", script])  # noqa: S603 - fixed test command
    try:
        time.sleep(1.5)
        descendants = collect_descendants(proc.pid, snapshot_processes())
        assert descendants, "expected at least one child process"
        terminate_process_tree(proc.pid)
        time.sleep(1.0)
        assert is_pid_alive(proc.pid) is False
        assert all(not is_pid_alive(pid) for pid in descendants)
    finally:
        if is_pid_alive(proc.pid):
            terminate_process_tree(proc.pid)


def test_assert_no_residual_processes_clean() -> None:
    script = (
        "import subprocess, sys, time; "
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
        "time.sleep(60)"
    )
    proc = subprocess.Popen([sys.executable, "-c", script])  # noqa: S603 - fixed test command
    try:
        time.sleep(1.5)
        clean, residual = assert_no_residual_processes(proc.pid)
        assert clean is True
        assert residual == []
    finally:
        if is_pid_alive(proc.pid):
            terminate_process_tree(proc.pid)


def test_assert_no_residual_processes_detects_survivor(monkeypatch) -> None:
    """A fake surviving 'python' descendant must be reported (fake-process simulation)."""

    def fake_snapshot() -> list[dict]:
        return [
            {"pid": 100, "ppid": 1, "name": "python"},
            {"pid": 200, "ppid": 100, "name": "git"},
            {"pid": 300, "ppid": 100, "name": "python"},
            {"pid": 400, "ppid": 1, "name": "node"},
        ]

    monkeypatch.setattr("control_plane.runtime.snapshot_processes", fake_snapshot)
    clean, residual = assert_no_residual_processes(100)
    assert clean is False
    assert any("python (pid 300)" in entry for entry in residual)
    assert not any("node (pid 400)" in entry for entry in residual)


@pytest.mark.skipif(sys.platform != "win32", reason="WMI snapshot is Windows-only")
def test_snapshot_and_collect_descendants_shape() -> None:
    snapshot = snapshot_processes()
    assert isinstance(snapshot, list)
    assert all({"pid", "ppid", "name"} <= set(row) for row in snapshot)
    descendants = collect_descendants(os.getpid(), snapshot)
    assert isinstance(descendants, list)
