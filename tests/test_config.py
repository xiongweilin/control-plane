from pathlib import Path

from control_plane.config import ControlPlaneConfig


def test_repo_allowlist_is_profile_specific(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    denied = tmp_path / "denied"
    cfg = ControlPlaneConfig(api_key="x", allowed_repo_roots=(str(allowed),))
    assert cfg.repo_allowed(allowed / "repo")
    assert not cfg.repo_allowed(denied / "repo")
