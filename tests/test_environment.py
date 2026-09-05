from pathlib import Path

import pytest

from control_plane.config import ControlPlaneConfig
from control_plane.environment import EnvironmentInspectionProvider, evaluate_environment


def make_config(tmp_path: Path) -> ControlPlaneConfig:
    return ControlPlaneConfig(
        api_key="test-key",
        codex_cli=tmp_path / "codex.cmd",
        windows_scan_roots=(),
        v2rayn_expected_path=r"D:\agent\v2rayN-windows-64\v2rayN.exe",
        cloud_ssh_target="cloud.example",
        cloud_protected_root="/srv/protected",
        cloud_tailscale_profile="/var/lib/tailscale/tailscaled.state",
        docker_build_cache_max_bytes=1024,
        automatic_handling_enabled=True,
        game_mode_enabled=False,
    )


def test_environment_evaluation_covers_fail_safe_findings(tmp_path: Path) -> None:
    snapshot = evaluate_environment(
        make_config(tmp_path),
        {
            "win_defend_status": "Stopped",
            "listeners": [
                {"address": "0.0.0.0", "port": 23443},
                {"address": "0.0.0.0", "port": 445},
            ],
            "recursive_scan_access_errors": 3,
            "docker_available": True,
            "docker_exited_count": 2,
            "docker_build_cache_bytes": 2048,
            "v2rayn_running": True,
            "v2rayn_path": r"C:\stale\v2rayN.exe",
            "cloud": {
                "root_checked": False,
                "root_readable": False,
                "tailscale_profile_present": False,
                "swap_present": False,
                "selinux_mode": "permissive",
                "cve_findings": 1,
            },
        },
        provider_health={"codex-primary": {"available": False}},
    )

    findings = {item.name: item for item in snapshot.observations if item.status == "problem"}
    assert {
        "codex_primary",
        "windows_defender",
        "ditto_listener",
        "smb_rpc_listeners",
        "windows_recursive_scan",
        "docker_exited_containers",
        "docker_build_cache",
        "v2rayn_path",
        "cloud_protected_root",
        "cloud_tailscale_profile",
        "cloud_swap",
        "cloud_selinux",
        "cloud_cve",
    } <= findings.keys()
    assert findings["windows_defender"].automation == "fail-safe"
    assert findings["docker_build_cache"].metadata["bytes"] == 2048
    assert any(
        item.name == "third_party_protection" and item.status == "unknown"
        for item in snapshot.observations
    )


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
    assert observations["synchronization"].automation == "fail-safe"
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
    assert {item.name for item in first.unknowns} >= {
        "windows_defender",
        "docker_exited_containers",
        "cloud_swap",
    }
    health = await provider.health()
    assert health.available is True
    assert "read-only" in health.detail
