from __future__ import annotations

from control_plane.codex_runner import wsl_path_to_windows


def test_wsl_path_to_windows_drive() -> None:
    assert wsl_path_to_windows("/mnt/d/download/agent") == "D:\\download\\agent"


def test_wsl_path_to_windows_unc() -> None:
    assert (
        wsl_path_to_windows("/srv/stack/dify", "Ubuntu-22.04")
        == "\\\\wsl.localhost\\Ubuntu-22.04\\srv\\stack\\dify"
    )
