from __future__ import annotations

from pathlib import Path

from control_plane.config import resolve_codex_cli
from control_plane.config import ControlPlaneConfig


def test_resolve_explicit_config_wins(monkeypatch, tmp_path) -> None:
    explicit = tmp_path / "custom-codex.cmd"
    explicit.write_text("", encoding="ascii")
    monkeypatch.delenv("APPDATA", raising=False)
    assert resolve_codex_cli(str(explicit)) == explicit


def test_resolve_falls_back_to_path_shim(monkeypatch) -> None:
    shim = Path("C:\\shims\\codex.cmd")
    monkeypatch.setattr(
        "control_plane.config.shutil.which",
        lambda name: str(shim) if name.startswith("codex") else None,
    )
    assert resolve_codex_cli() == shim


def test_resolve_all_missing_returns_bare_codex(monkeypatch) -> None:
    monkeypatch.setattr("control_plane.config.shutil.which", lambda name: None)
    assert resolve_codex_cli() == Path("codex")


def test_load_codex_process_boundary_options(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("CONTROL_PLANE_API_KEY", "test-key")
    config_path = tmp_path / "control_plane.toml"
    config_path.write_text(
        "[agent]\n"
        "isolate_worktree = false\n"
        "disable_docker = false\n"
        "disable_ssh_credentials = false\n"
        f'worktree_root = "{(tmp_path / "worktrees").as_posix()}"\n',
        encoding="utf-8",
    )
    config = ControlPlaneConfig.load(config_path)
    assert config.codex_isolate_worktree is False
    assert config.codex_disable_docker is False
    assert config.codex_disable_ssh_credentials is False
    assert config.codex_worktree_root == tmp_path / "worktrees"
