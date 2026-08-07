from __future__ import annotations

from control_plane.codex_runner import repo_path_to_windows


def test_repo_path_to_windows_passthrough() -> None:
    assert repo_path_to_windows("D:\\download\\agent\\control-plane") == "D:\\download\\agent\\control-plane"


def test_repo_path_to_windows_forward_slashes() -> None:
    assert repo_path_to_windows("D:/download/agent/control-plane") == "D:\\download\\agent\\control-plane"
