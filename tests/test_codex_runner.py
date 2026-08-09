from __future__ import annotations

from pathlib import Path

import pytest

from control_plane.codex_runner import (
    CodexCliUnavailableError,
    CodexRunner,
    repo_path_to_windows,
)
from control_plane.config import ControlPlaneConfig


def test_repo_path_to_windows_passthrough() -> None:
    assert repo_path_to_windows("D:\\download\\agent\\control-plane") == "D:\\download\\agent\\control-plane"


def test_repo_path_to_windows_forward_slashes() -> None:
    assert repo_path_to_windows("D:/download/agent/control-plane") == "D:\\download\\agent\\control-plane"


class _FakeProc:
    def __init__(self, returncode: int, stdout: bytes = b"", stderr: bytes = b"") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _runner(codex_cli: Path) -> CodexRunner:
    return CodexRunner(ControlPlaneConfig(codex_cli=codex_cli))


def test_cli_info_returns_path_and_version(monkeypatch) -> None:
    runner = _runner(Path("C:\\tools\\codex.exe"))

    def fake_run(args, **kwargs):
        assert args == ["C:\\tools\\codex.exe", "--version"]
        return _FakeProc(0, b"codex-cli 0.145.0\n")

    monkeypatch.setattr("control_plane.codex_runner.subprocess.run", fake_run)
    path, version = runner.cli_info()
    assert path == Path("C:\\tools\\codex.exe")
    assert version == "codex-cli 0.145.0"


def test_cli_info_missing_binary_raises_clear_error(monkeypatch) -> None:
    runner = _runner(Path("Z:\\no-such\\codex.exe"))

    def fake_run(args, **kwargs):
        raise FileNotFoundError("no such file")

    monkeypatch.setattr("control_plane.codex_runner.subprocess.run", fake_run)
    with pytest.raises(CodexCliUnavailableError, match="not runnable"):
        runner.cli_info()


def test_cli_info_nonzero_exit_raises_clear_error(monkeypatch) -> None:
    runner = _runner(Path("codex"))
    monkeypatch.setattr(
        "control_plane.codex_runner.subprocess.run",
        lambda args, **kwargs: _FakeProc(1, b"", b"broken install"),
    )
    with pytest.raises(CodexCliUnavailableError, match="version probe failed"):
        runner.cli_info()


def test_run_task_fails_fast_when_cli_missing(monkeypatch) -> None:
    runner = _runner(Path("Z:\\no-such\\codex.exe"))
    monkeypatch.setattr(
        "control_plane.codex_runner.subprocess.run",
        lambda args, **kwargs: (_ for _ in ()).throw(FileNotFoundError("missing")),
    )
    with pytest.raises(CodexCliUnavailableError):
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
    store.set_setting("codex:cli_version", "codex-cli 0.144.0")
    runner = _runner(Path("C:\\tools\\codex.exe"))
    runner.attach_store(store)
    monkeypatch.setattr(
        "control_plane.codex_runner.subprocess.run",
        lambda args, **kwargs: _FakeProc(0, b"codex-cli 0.145.0\n"),
    )
    path, version, changed = runner.check_cli()
    assert changed is True
    assert version == "codex-cli 0.145.0"
    assert store.get_setting("codex:cli_version") == "codex-cli 0.145.0"
    assert store.get_setting("codex:cli_path") == "C:\\tools\\codex.exe"
    store.close()


def test_check_cli_no_change_on_same_version(monkeypatch, tmp_path) -> None:
    from control_plane.storage import Store

    store = Store(tmp_path / "cp.db")
    store.set_setting("codex:cli_version", "codex-cli 0.145.0")
    runner = _runner(Path("C:\\tools\\codex.exe"))
    runner.attach_store(store)
    monkeypatch.setattr(
        "control_plane.codex_runner.subprocess.run",
        lambda args, **kwargs: _FakeProc(0, b"codex-cli 0.145.0\n"),
    )
    _, _, changed = runner.check_cli()
    assert changed is False
    store.close()
