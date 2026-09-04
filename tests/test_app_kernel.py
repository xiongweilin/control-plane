from pathlib import Path

from control_plane.app import _split_controller_reply, create_app
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
    assert "personal-monitoring" in provider_ids
    assert "personal-operations" in provider_ids
    assert app.state.controller.runtime is runtime
    assert callable(app.state.controller.step)


def test_personal_effect_rules_are_kernel_owned(tmp_path: Path) -> None:
    app = create_app(make_config(tmp_path))
    registry = app.state.kernel_runtime.contract_registry
    assert registry.effect_rule("git.push").authorization_required is True
    assert registry.effect_rule("docker.restart").authorization_required is True
    assert registry.effect_rule("docker.compose.up").authorization_required is False
    assert registry.effect_rule("notify.send").authorization_required is False


def test_personal_task_and_waiting_command_surfaces_are_present(tmp_path: Path) -> None:
    app = create_app(make_config(tmp_path))
    paths = {route.path for route in app.routes}
    assert "/v1/tasks" in paths
    assert "/v1/controllers/{controller_id}/command" in paths
    assert "/v1/alerts/alertmanager" in paths
    assert "/status" in paths


def test_feishu_task_reply_can_address_waiting_controller() -> None:
    assert _split_controller_reply("controller_abc restart prometheus") == (
        "controller_abc",
        "restart prometheus",
    )
    assert _split_controller_reply("ordinary task") is None


def test_profile_models_are_frozen_to_requested_split(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    assert cfg.diagnosis_model == "codex/gpt-5.6-sol"
    assert cfg.execution_model == "codex/gpt-5.6-luna"
