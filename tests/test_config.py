from __future__ import annotations

from pathlib import Path

from control_plane.config import DSH_CLI_DEFAULT, resolve_dsh_cli


def test_resolve_explicit_config_wins(monkeypatch, tmp_path) -> None:
    explicit = tmp_path / "custom-dsh.js"
    explicit.write_text("", encoding="ascii")
    monkeypatch.delenv("APPDATA", raising=False)
    assert resolve_dsh_cli(str(explicit)) == explicit


def test_resolve_prefers_shared_checkout_default(monkeypatch, tmp_path) -> None:
    default = tmp_path / "dsh-varin" / "apps" / "cli" / "lib" / "bin.js"
    default.parent.mkdir(parents=True)
    default.write_text("", encoding="ascii")
    monkeypatch.setattr(
        "control_plane.config.DSH_CLI_DEFAULT", default
    )
    assert resolve_dsh_cli() == default


def test_resolve_falls_back_to_path_shim(monkeypatch, tmp_path) -> None:
    missing = tmp_path / "missing" / "bin.js"
    monkeypatch.setattr("control_plane.config.shutil.which", lambda name: None)
    monkeypatch.setattr("control_plane.config.DSH_CLI_DEFAULT", missing)
    shim = Path("C:\\shims\\dsh.cmd")
    monkeypatch.setattr(
        "control_plane.config.shutil.which",
        lambda name: str(shim) if name.startswith("dsh") else None,
    )
    assert resolve_dsh_cli() == shim


def test_resolve_all_missing_returns_bare_dsh(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("control_plane.config.shutil.which", lambda name: None)
    monkeypatch.setattr(
        "control_plane.config.DSH_CLI_DEFAULT", tmp_path / "missing" / "bin.js"
    )
    assert resolve_dsh_cli() == Path("dsh")


def test_default_points_to_shared_checkout() -> None:
    assert Path(
        r"D:\download\agent\dsh-varin\apps\cli\lib\bin.js"
    ) == DSH_CLI_DEFAULT
