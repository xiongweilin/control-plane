from __future__ import annotations

from pathlib import Path

from control_plane.config import resolve_codex_cli


def test_resolve_explicit_config_wins(monkeypatch, tmp_path) -> None:
    explicit = tmp_path / "custom-codex.exe"
    explicit.write_text("", encoding="ascii")
    monkeypatch.delenv("APPDATA", raising=False)
    assert resolve_codex_cli(str(explicit)) == explicit


def test_resolve_prefers_path_shim_over_bare_fallback(monkeypatch) -> None:
    shim = Path("C:\\shims\\codex.exe")
    monkeypatch.setattr(
        "control_plane.config.shutil.which",
        lambda name: str(shim) if name.startswith("codex") else None,
    )
    assert resolve_codex_cli() == shim


def test_resolve_falls_back_to_npm_vendor_exe(monkeypatch, tmp_path) -> None:
    vendor_exe = (
        tmp_path
        / "npm"
        / "node_modules"
        / "@openai"
        / "codex"
        / "node_modules"
        / "@openai"
        / "codex-win32-x64"
        / "vendor"
        / "x86_64-pc-windows-msvc"
        / "bin"
        / "codex.exe"
    )
    vendor_exe.parent.mkdir(parents=True)
    vendor_exe.write_text("", encoding="ascii")
    monkeypatch.setattr("control_plane.config.shutil.which", lambda name: None)
    monkeypatch.setenv("APPDATA", str(tmp_path))
    assert resolve_codex_cli() == vendor_exe


def test_resolve_npm_cmd_shim_prefers_native_exe(monkeypatch, tmp_path) -> None:
    vendor_exe = (
        tmp_path
        / "npm"
        / "node_modules"
        / "@openai"
        / "codex"
        / "node_modules"
        / "@openai"
        / "codex-win32-x64"
        / "vendor"
        / "x86_64-pc-windows-msvc"
        / "bin"
        / "codex.exe"
    )
    vendor_exe.parent.mkdir(parents=True)
    vendor_exe.write_text("", encoding="ascii")
    monkeypatch.setattr(
        "control_plane.config.shutil.which",
        lambda name: "C:\\npm\\codex.cmd" if name == "codex" else None,
    )
    monkeypatch.setenv("APPDATA", str(tmp_path))
    assert resolve_codex_cli() == vendor_exe


def test_resolve_all_missing_returns_bare_codex(monkeypatch) -> None:
    monkeypatch.setattr("control_plane.config.shutil.which", lambda name: None)
    monkeypatch.setenv("APPDATA", "Z:\\no\\appdata")
    assert resolve_codex_cli() == Path("codex")
