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


def test_loads_dispatch_settings_from_their_config_sections(
    tmp_path: Path, monkeypatch
) -> None:
    config_path = tmp_path / "control-plane.toml"
    config_path.write_text(
        """
[server]
host = "0.0.0.0"
port = 18083
[agent]
gateway_timeout_seconds = 42
max_agent_calls_per_repair = 5
[policy]
cooldown_seconds = 17
max_attempts = 3
per_repair_timeout_seconds = 88
max_concurrent = 4
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("CONTROL_PLANE_API_KEY", "test-key")

    cfg = ControlPlaneConfig.load(config_path)

    assert cfg.host == "0.0.0.0"
    assert cfg.gateway_timeout_seconds == 42
    assert cfg.max_agent_calls_per_repair == 5
    assert cfg.cooldown_seconds == 17
    assert cfg.max_attempts == 3
    assert cfg.per_repair_timeout_seconds == 88
    assert cfg.max_concurrent == 4
