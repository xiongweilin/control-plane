from pathlib import Path

from control_plane.app import create_app
from control_plane.config import ControlPlaneConfig


def make_config(tmp_path: Path) -> ControlPlaneConfig:
    return ControlPlaneConfig(
        api_key="test-key",
        state_db=tmp_path / "kernel.db",
        artifact_root=tmp_path / "artifacts",
        agent_session_dir=tmp_path / "sessions",
        codex_worktree_root=tmp_path / "worktrees",
        prometheus_url="",
        alertmanager_url="",
        notification_enabled=False,
        allowed_repo_roots=(str(tmp_path),),
        project_dirs={"test": str(tmp_path)},
        allowed_auto_projects=("test",),
    )


def test_app_is_thin_agent_kernel_profile(tmp_path: Path) -> None:
    app = create_app(make_config(tmp_path))
    runtime = app.state.kernel_runtime
    assert runtime.runtime_id == "personal-platform"
    provider_ids = {item.id for item in runtime.registry.list()}
    assert "codex-primary" in provider_ids
    assert "feishu-human" in provider_ids
    assert "personal-operations" in provider_ids
    assert app.state.controller.runtime is runtime


def test_personal_effect_rules_are_kernel_owned(tmp_path: Path) -> None:
    app = create_app(make_config(tmp_path))
    registry = app.state.kernel_runtime.contract_registry
    assert registry.effect_rule("git.push").authorization_required is True
    assert registry.effect_rule("docker.restart").authorization_required is True
    assert registry.effect_rule("notify.send").authorization_required is False
