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
