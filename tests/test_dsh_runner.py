from __future__ import annotations

import os
from pathlib import Path

import pytest

from control_plane.config import ControlPlaneConfig
from control_plane.dsh_runner import (
    DshCliUnavailableError,
    DshRunner,
    repo_path_to_windows,
)


def test_repo_path_to_windows_passthrough() -> None:
    assert repo_path_to_windows("D:\\download\\agent\\control-plane") == "D:\\download\\agent\\control-plane"


def test_repo_path_to_windows_forward_slashes() -> None:
    expected = "D:\\download\\agent\\control-plane" if os.name == "nt" else "D:/download/agent/control-plane"
    assert repo_path_to_windows("D:/download/agent/control-plane") == expected


class _FakeProc:
    def __init__(self, returncode: int, stdout: bytes = b"", stderr: bytes = b"") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _runner(dsh_cli: Path) -> DshRunner:
    return DshRunner(ControlPlaneConfig(dsh_cli=dsh_cli))


def test_cli_info_returns_path_and_version(monkeypatch) -> None:
    runner = _runner(Path("C:\\tools\\dsh.cmd"))

    def fake_run(args, **kwargs):
        assert args == ["C:\\tools\\dsh.cmd", "--version"]
        return _FakeProc(0, b"dsh-cli 0.1.0-rc.5\n")

    monkeypatch.setattr("control_plane.dsh_runner.subprocess.run", fake_run)
    path, version = runner.cli_info()
    assert path == Path("C:\\tools\\dsh.cmd")
    assert version == "dsh-cli 0.1.0-rc.5"


def test_cli_info_js_entry_runs_through_node(monkeypatch) -> None:
    runner = _runner(Path("D:\\tools\\deepseek-harness\\apps\\cli\\lib\\bin.js"))
    monkeypatch.setattr(
        "control_plane.dsh_runner.shutil.which", lambda name: "node"
    )

    def fake_run(args, **kwargs):
        assert args == [
            "node",
            "D:\\tools\\deepseek-harness\\apps\\cli\\lib\\bin.js",
            "--version",
        ]
        return _FakeProc(0, b"0.1.0-rc.5\n")

    monkeypatch.setattr("control_plane.dsh_runner.subprocess.run", fake_run)
    _, version = runner.cli_info()
    assert version == "0.1.0-rc.5"


def test_cli_info_missing_binary_raises_clear_error(monkeypatch) -> None:
    runner = _runner(Path("Z:\\no-such\\dsh.cmd"))

    def fake_run(args, **kwargs):
        raise FileNotFoundError("no such file")

    monkeypatch.setattr("control_plane.dsh_runner.subprocess.run", fake_run)
    with pytest.raises(DshCliUnavailableError, match="not runnable"):
        runner.cli_info()


def test_cli_info_nonzero_exit_raises_clear_error(monkeypatch) -> None:
    runner = _runner(Path("dsh"))
    monkeypatch.setattr(
        "control_plane.dsh_runner.subprocess.run",
        lambda args, **kwargs: _FakeProc(1, b"", b"broken install"),
    )
    with pytest.raises(DshCliUnavailableError, match="version probe failed"):
        runner.cli_info()


def test_run_task_fails_fast_when_cli_missing(monkeypatch) -> None:
    runner = _runner(Path("Z:\\no-such\\dsh.cmd"))
    monkeypatch.setattr(
        "control_plane.dsh_runner.subprocess.run",
        lambda args, **kwargs: (_ for _ in ()).throw(FileNotFoundError("missing")),
    )
    with pytest.raises(DshCliUnavailableError):
        import asyncio

        asyncio.run(
            runner.run_task(
                repair_id="repair-x",
                repo="D:\\repo",
                prompt="hi",
            )
        )


def test_check_cli_detects_version_change(monkeypatch, tmp_path) -> None:
    from control_plane.storage import Store

    store = Store(tmp_path / "cp.db")
    store.set_setting("dsh:cli_version", "dsh-cli 0.1.0-rc.4")
    runner = _runner(Path("C:\\tools\\dsh.cmd"))
    runner.attach_store(store)
    monkeypatch.setattr(
        "control_plane.dsh_runner.subprocess.run",
        lambda args, **kwargs: _FakeProc(0, b"dsh-cli 0.1.0-rc.5\n"),
    )
    path, version, changed = runner.check_cli()
    assert changed is True
    assert version == "dsh-cli 0.1.0-rc.5"
    assert store.get_setting("dsh:cli_version") == "dsh-cli 0.1.0-rc.5"
    assert store.get_setting("dsh:cli_path") == "C:\\tools\\dsh.cmd"
    store.close()


def test_check_cli_no_change_on_same_version(monkeypatch, tmp_path) -> None:
    from control_plane.storage import Store

    store = Store(tmp_path / "cp.db")
    store.set_setting("dsh:cli_version", "dsh-cli 0.1.0-rc.5")
    runner = _runner(Path("C:\\tools\\dsh.cmd"))
    runner.attach_store(store)
    monkeypatch.setattr(
        "control_plane.dsh_runner.subprocess.run",
        lambda args, **kwargs: _FakeProc(0, b"dsh-cli 0.1.0-rc.5\n"),
    )
    _, _, changed = runner.check_cli()
    assert changed is False
    store.close()
