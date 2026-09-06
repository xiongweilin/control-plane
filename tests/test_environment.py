from pathlib import Path

import pytest

from control_plane.config import ControlPlaneConfig
from control_plane.environment import EnvironmentInspectionProvider, evaluate_environment


def make_config(tmp_path: Path) -> ControlPlaneConfig:
    return ControlPlaneConfig(
        api_key="test-key",
        codex_cli=tmp_path / "codex.cmd",
        v2rayn_expected_path=r"D:\agent\v2rayN-windows-64\v2rayN.exe",
        docker_build_cache_max_bytes=1024,
        docker_expected_exited_containers=("dify-init_permissions-1",),
        automatic_handling_enabled=True,
        game_mode_enabled=False,
    )


def test_environment_evaluation_covers_codex_judgment_findings(tmp_path: Path) -> None:
    snapshot = evaluate_environment(
        make_config(tmp_path),
        {
            "docker_available": True,
            "docker_exited_count": 2,
            "docker_build_cache_bytes": 2048,
            "v2rayn_running": True,
            "v2rayn_path": r"C:\stale\v2rayN.exe",
        },
        provider_health={"codex-primary": {"available": False}},
    )

    findings = {item.name: item for item in snapshot.observations if item.status == "problem"}
    assert {
        "codex_primary",
        "docker_exited_containers",
        "docker_build_cache",
        "v2rayn_path",
    } <= findings.keys()
    assert findings["docker_build_cache"].metadata["bytes"] == 2048
    assert not {item.name for item in snapshot.observations} & {
        "windows_defender",
        "third_party_protection",
        "ditto_listener",
        "smb_rpc_listeners",
        "windows_recursive_scan",
        "cloud_protected_root",
        "cloud_tailscale_profile",
        "cloud_swap",
        "cloud_selinux",
        "cloud_cve",
    }


def test_environment_evaluation_covers_standing_control_plane_responsibilities(
    tmp_path: Path,
) -> None:
    config = make_config(tmp_path)
    snapshot = evaluate_environment(
        config,
        {
            "recovery_ok": False,
            "recovery_missing_paths": ["D:/agent/docker备份"],
            "synchronization_ok": False,
            "synchronization_failures": ["D:/agent/ratio"],
            "synchronization_checked": 2,
            "known_garbage_count": 1,
            "known_garbage_paths": ["D:/agent/portable-runtime-worktrees"],
        },
        provider_health={"personal-operations": {"available": True}},
    )
    observations = {item.name: item for item in snapshot.observations}

    assert observations["recoverability"].status == "problem"
    assert observations["recoverability"].severity == "critical"
    assert observations["synchronization"].status == "problem"
    assert observations["synchronization"].automation == "codex-judgment"
    assert observations["known_garbage"].status == "problem"
    assert observations["known_garbage"].automation == "automatic"
    assert observations["automatic_handling"].status == "ok"


def test_game_mode_treats_docker_down_as_expected_state(tmp_path: Path) -> None:
    snapshot = evaluate_environment(
        make_config(tmp_path),
        {"docker_available": False},
        game_mode_suppresses_docker=True,
    )
    observations = {item.name: item for item in snapshot.observations}

    assert observations["docker_exited_containers"].status == "ok"
    assert observations["docker_build_cache"].status == "ok"
    assert observations["docker_exited_containers"].metadata["expected_down"] is True


def test_expected_one_shot_containers_do_not_become_unexpected_exited_alerts(
    tmp_path: Path,
) -> None:
    snapshot = evaluate_environment(
        make_config(tmp_path),
        {
            "docker_available": True,
            "docker_exited_count": 1,
            "docker_exited_container_names": ["dify-init_permissions-1"],
            "docker_build_cache_bytes": 0,
        },
    )
    observation = {item.name: item for item in snapshot.observations}["docker_exited_containers"]
    assert observation.status == "ok"
    assert observation.metadata["expected_down"] is True


def test_missing_v2rayn_process_keeps_path_fact_separate_from_status(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    expected = Path(config.v2rayn_expected_path or "")
    expected.parent.mkdir(parents=True, exist_ok=True)
    expected.touch()
    snapshot = evaluate_environment(config, {"v2rayn_running": False, "v2rayn_path": ""})
    observations = {item.name: item for item in snapshot.observations}
    assert observations["v2rayn_path"].status == "ok"
    assert observations["v2rayn_status"].status == "problem"


@pytest.mark.asyncio
async def test_environment_provider_is_read_only_and_cacheable(tmp_path: Path) -> None:
    calls = 0

    def probe() -> dict[str, object]:
        nonlocal calls
        calls += 1
        return {"docker_available": False}

    config = make_config(tmp_path)
    provider = EnvironmentInspectionProvider(config, probe_runner=probe)
    first = await provider.refresh()
    second = await provider.refresh()

    assert first is second
    assert calls == 1
    assert {item.name for item in first.unknowns} >= {"docker_exited_containers"}
    assert not {item.name for item in first.unknowns} & {
        "windows_defender",
        "third_party_protection",
        "ditto_listener",
        "smb_rpc_listeners",
        "windows_recursive_scan",
        "cloud_protected_root",
        "cloud_tailscale_profile",
        "cloud_swap",
        "cloud_selinux",
        "cloud_cve",
    }
    health = await provider.health()
    assert health.available is True
    assert "read-only" in health.detail
