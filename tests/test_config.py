from pathlib import Path

from control_plane.config import ControlPlaneConfig


def test_repo_allowlist_is_profile_specific(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    denied = tmp_path / "denied"
    cfg = ControlPlaneConfig(api_key="x", allowed_repo_roots=(str(allowed),))
    assert cfg.repo_allowed(allowed / "repo")
    assert not cfg.repo_allowed(denied / "repo")


def test_auto_project_requires_exact_configured_repo(tmp_path: Path) -> None:
    project = tmp_path / "project"
    cfg = ControlPlaneConfig(
        api_key="x",
        allowed_auto_projects=("project",),
        project_dirs={"project": str(project)},
        allowed_repo_roots=(str(tmp_path),),
    )
    assert cfg.auto_project_for_repo(project) == "project"
    assert cfg.auto_project_for_repo(project / "nested") is None
