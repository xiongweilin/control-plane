from __future__ import annotations

from pathlib import Path

from control_plane.config import resolve_codex_cli


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
