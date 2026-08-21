from __future__ import annotations

import pytest

from control_plane.tools import ToolError, resolve_repo, validate_url


def test_validate_url_origin() -> None:
    assert validate_url("https://metratio.com/feedback", ("https://metratio.com",)) == "https://metratio.com/feedback"
    with pytest.raises(ToolError):
        validate_url("https://evil.example.com/x", ("https://metratio.com",))
    with pytest.raises(ToolError):
        validate_url("https://metratio.com.evil.example/x", ("https://metratio.com",))
    with pytest.raises(ToolError):
        validate_url("https://user:secret@metratio.com/x", ("https://metratio.com",))
    with pytest.raises(ToolError):
        validate_url("https://metratio.com:444/x", ("https://metratio.com",))


def test_resolve_repo_allowed_and_denied() -> None:
    allowed = ("D:\\infrastructure\\compose", "D:\\agent")
    assert resolve_repo("D:\\infrastructure\\compose\\dify", allowed) == "D:/infrastructure/compose/dify"
    assert resolve_repo("D:/agent/control-plane", allowed) == "D:/agent/control-plane"
    with pytest.raises(ToolError):
        resolve_repo("C:\\Windows\\System32", allowed)


def test_resolve_repo_blocked_paths() -> None:
    allowed = ("D:\\infrastructure\\compose",)
    with pytest.raises(ToolError, match="blocked by policy"):
        resolve_repo("D:\\infrastructure\\compose\\secrets-holder", allowed, blocked=("secrets",))
    with pytest.raises(ToolError, match="blocked by policy"):
        resolve_repo("D:\\infrastructure\\compose\\app\\config\\.env", allowed, blocked=(".env",))
    with pytest.raises(ToolError, match="blocked by policy"):
        resolve_repo(
            "D:\\infrastructure\\compose\\app\\key.pem",
            allowed,
            blocked=(".env", ".pem", ".key"),
        )
    with pytest.raises(ToolError, match="blocked by policy"):
        resolve_repo(
            "D:\\infrastructure\\compose\\app\\token.txt",
            allowed,
            blocked=("token",),
        )
    # .env.example 只含占位符，不应被 .env 子串误伤（2026-08-17 边界匹配修正）。
    assert (
        resolve_repo(
            "D:\\infrastructure\\compose\\app\\config\\.env.example",
            allowed,
            blocked=(".env",),
        )
        == "D:/infrastructure/compose/app/config/.env.example"
    )
    assert (
        resolve_repo("D:\\infrastructure\\compose\\app", allowed, blocked=(".env",))
        == "D:/infrastructure/compose/app"
    )


def test_resolve_repo_uses_canonical_containment(tmp_path) -> None:
    root = tmp_path / "repos"
    inside = root / "app"
    outside = tmp_path / "outside"
    inside.mkdir(parents=True)
    outside.mkdir()
    assert resolve_repo(str(root / "app" / ".." / "app"), (str(root),)) == str(inside).replace("\\", "/")
    with pytest.raises(ToolError):
        resolve_repo(str(root / ".." / "outside"), (str(root),))
    link = root / "escape"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlink unavailable: {exc}")
    with pytest.raises(ToolError):
        resolve_repo(str(link), (str(root),))
