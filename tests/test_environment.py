import json
import subprocess
import time
from dataclasses import replace
from pathlib import Path

import pytest

import control_plane.environment as environment
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


def make_lifecycle_config(tmp_path: Path, repo: Path, chezmoi: Path) -> ControlPlaneConfig:
    return replace(
        make_config(tmp_path),
        recovery_paths=(str(tmp_path),),
        synchronization_paths=(str(repo),),
        chezmoi_source_dir=str(chezmoi),
        known_garbage_paths=(),
        project_dirs={"sample": str(repo), "chezmoi": str(chezmoi)},
    )


def completed(
    args: list[str],
    *,
    returncode: int = 0,
    stdout: bytes = b"",
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.CompletedProcess(args, returncode, stdout, b"")


def find_probe_tool(name: str) -> str | None:
    candidate = name.removesuffix(".exe")
    return candidate if candidate in {"git", "chezmoi"} else None


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


def test_remote_sha_cache_handles_missing_invalid_and_expired_entries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache_path = tmp_path / "remote-sha-cache.json"
    monkeypatch.setattr(environment, "REMOTE_SHA_CACHE_PATH", cache_path)

    assert environment._load_remote_cache() == {}

    cache_path.write_bytes(b"not-json")
    assert environment._load_remote_cache() == {}

    cache_path.write_text(json.dumps({"entries": []}), encoding="utf-8")
    assert environment._load_remote_cache() == {}

    now = time.time()
    cache_path.write_text(
        json.dumps(
            {
                "entries": {
                    "fresh": {"sha": "abc", "checked_at": now},
                    "expired": {
                        "sha": "expired",
                        "checked_at": now - environment.REMOTE_SHA_CACHE_TTL_SECONDS - 1,
                    },
                    "future": {"sha": "future", "checked_at": now + 1},
                    "malformed": {"sha": "bad", "checked_at": "unknown"},
                    "not-an-entry": "bad",
                }
            }
        ),
        encoding="utf-8",
    )
    cache = environment._load_remote_cache()

    assert environment._cached_remote_sha(cache, "fresh") == "abc"
    assert environment._cached_remote_sha(cache, "expired") == ""
    assert environment._cached_remote_sha(cache, "future") == ""
    assert environment._cached_remote_sha(cache, "malformed") == ""
    assert environment._cached_remote_sha(cache, "not-an-entry") == ""
    assert environment._cached_remote_sha(cache, "missing") == ""


def test_lifecycle_probe_reports_fresh_repository_and_chezmoi_subjects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    chezmoi = tmp_path / "chezmoi"
    chezmoi.mkdir()
    raw_repo = str(repo)
    head_sha = "a" * 40
    provider = EnvironmentInspectionProvider(make_lifecycle_config(tmp_path, repo, chezmoi))

    monkeypatch.setattr(
        environment.shutil,
        "which",
        find_probe_tool,
    )
    monkeypatch.setattr(
        environment,
        "_load_remote_cache",
        lambda: {raw_repo: {"sha": head_sha, "checked_at": time.time()}},
    )

    def run_command(args: list[str], *, timeout: float) -> subprocess.CompletedProcess[bytes]:
        del timeout
        if "status" in args:
            return completed(args)
        if "rev-parse" in args:
            return completed(args, stdout=f"{head_sha}\n".encode())
        if "verify" in args:
            return completed(args)
        raise AssertionError(f"unexpected probe command: {args}")

    monkeypatch.setattr(provider, "_run_bounded_command", run_command)
    result = provider._run_lifecycle_probe()

    assert result["recovery_ok"] is True
    assert result["synchronization_ok"] is None
    assert result["synchronization_checked"] == 1
    subjects = result["synchronization_subjects"]
    assert subjects[0] == {
        "path": raw_repo,
        "project": "sample",
        "remote": "origin",
        "branch": "main",
        "head_sha": head_sha,
        "remote_sha": head_sha,
        "worktree": "clean",
        "status": "ok",
        "reason": "",
    }
    assert subjects[1]["path"] == str(chezmoi)
    assert subjects[1]["project"] == "chezmoi"
    assert subjects[1]["chezmoi_verify"] == "ok"


def test_lifecycle_probe_marks_stale_cache_unknown_and_starts_refresh(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    chezmoi = tmp_path / "chezmoi"
    chezmoi.mkdir()
    raw_repo = str(repo)
    head_sha = "b" * 40
    provider = EnvironmentInspectionProvider(make_lifecycle_config(tmp_path, repo, chezmoi))
    refreshed: list[list[str]] = []

    monkeypatch.setattr(
        environment.shutil,
        "which",
        find_probe_tool,
    )
    monkeypatch.setattr(environment, "_load_remote_cache", lambda: {})
    monkeypatch.setattr(provider, "_spawn_remote_refresh", lambda paths: refreshed.append(paths))

    def run_command(args: list[str], *, timeout: float) -> subprocess.CompletedProcess[bytes]:
        del timeout
        if "status" in args:
            return completed(args)
        if "rev-parse" in args:
            return completed(args, stdout=f"{head_sha}\n".encode())
        if "verify" in args:
            return completed(args)
        raise AssertionError(f"unexpected probe command: {args}")

    monkeypatch.setattr(provider, "_run_bounded_command", run_command)
    result = provider._run_lifecycle_probe()

    assert result["synchronization_ok"] is None
    assert result["synchronization_failures"] == []
    assert result["synchronization_subjects"][0]["status"] == "unknown"
    assert result["synchronization_subjects"][0]["reason"] == "remote_cache_stale"
    assert refreshed == [[raw_repo]]


@pytest.mark.parametrize(
    ("status_returncode", "status_stdout", "head_returncode", "reason", "worktree"),
    [
        (0, b" M changed.txt\n", 0, "uncommitted_worktree", "dirty"),
        (1, b"", 0, "worktree_status_failed", "unknown"),
        (0, b"", 1, "local_head_unreadable", "clean"),
    ],
)
def test_lifecycle_probe_reports_local_sync_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status_returncode: int,
    status_stdout: bytes,
    head_returncode: int,
    reason: str,
    worktree: str,
) -> None:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    chezmoi = tmp_path / "chezmoi"
    chezmoi.mkdir()
    provider = EnvironmentInspectionProvider(make_lifecycle_config(tmp_path, repo, chezmoi))
    monkeypatch.setattr(
        environment.shutil,
        "which",
        find_probe_tool,
    )
    monkeypatch.setattr(environment, "_load_remote_cache", lambda: {})

    def run_command(args: list[str], *, timeout: float) -> subprocess.CompletedProcess[bytes]:
        del timeout
        if "status" in args:
            return completed(args, returncode=status_returncode, stdout=status_stdout)
        if "rev-parse" in args:
            return completed(args, returncode=head_returncode, stdout=b"c" * 40)
        if "verify" in args:
            return completed(args)
        raise AssertionError(f"unexpected probe command: {args}")

    monkeypatch.setattr(provider, "_run_bounded_command", run_command)
    result = provider._run_lifecycle_probe()

    assert result["synchronization_ok"] is False
    assert result["synchronization_failures"] == [str(repo)]
    assert result["synchronization_failure_reasons"] == [{"path": str(repo), "reason": reason}]
    assert result["synchronization_subjects"][0]["worktree"] == worktree
    assert result["synchronization_subjects"][0]["reason"] == reason


def test_lifecycle_probe_reports_missing_git_and_chezmoi(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "missing-repo"
    chezmoi = tmp_path / "chezmoi"
    chezmoi.mkdir()
    provider = EnvironmentInspectionProvider(make_lifecycle_config(tmp_path, repo, chezmoi))
    monkeypatch.setattr(environment.shutil, "which", lambda _: None)

    result = provider._run_lifecycle_probe()

    assert result["synchronization_checked"] == 0
    assert result["synchronization_ok"] is False
    assert result["synchronization_failures"] == [str(repo), str(chezmoi)]
    assert result["synchronization_failure_reasons"] == [
        {"path": str(repo), "reason": "not_git_repository_or_git_unavailable"},
        {"path": str(chezmoi), "reason": "chezmoi_unavailable"},
    ]


def test_lifecycle_probe_reports_remote_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    chezmoi = tmp_path / "chezmoi"
    chezmoi.mkdir()
    raw_repo = str(repo)
    provider = EnvironmentInspectionProvider(make_lifecycle_config(tmp_path, repo, chezmoi))
    monkeypatch.setattr(
        environment.shutil,
        "which",
        find_probe_tool,
    )
    monkeypatch.setattr(
        environment,
        "_load_remote_cache",
        lambda: {raw_repo: {"sha": "d" * 40, "checked_at": time.time()}},
    )

    def run_command(args: list[str], *, timeout: float) -> subprocess.CompletedProcess[bytes]:
        del timeout
        if "status" in args:
            return completed(args)
        if "rev-parse" in args:
            return completed(args, stdout=("e" * 40 + "\n").encode())
        if "verify" in args:
            return completed(args)
        raise AssertionError(f"unexpected probe command: {args}")

    monkeypatch.setattr(provider, "_run_bounded_command", run_command)
    result = provider._run_lifecycle_probe()

    assert result["synchronization_ok"] is False
    assert result["synchronization_failure_reasons"] == [
        {"path": raw_repo, "reason": "local_head_or_origin_main_mismatch"}
    ]
    assert result["synchronization_subjects"][0]["status"] == "problem"


def test_remote_refresh_writes_sha_cache_and_is_rate_limited(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    cache_path = tmp_path / "remote-sha-cache.json"
    raw_repo = str(repo)
    remote_sha = "f" * 40
    provider = EnvironmentInspectionProvider(make_config(tmp_path))
    monkeypatch.setattr(environment, "REMOTE_SHA_CACHE_PATH", cache_path)
    monkeypatch.setattr(environment, "_load_remote_cache", lambda: {"existing": {"sha": "old"}})
    monkeypatch.setattr(environment.shutil, "which", lambda name: "git" if name == "git" else None)

    class ImmediateThread:
        def __init__(self, target, **_: object) -> None:
            self.target = target

        def start(self) -> None:
            self.target()

    monkeypatch.setattr(environment.threading, "Thread", ImmediateThread)
    calls: list[list[str]] = []

    def run_remote(args: list[str], **_: object) -> subprocess.CompletedProcess[bytes]:
        calls.append(args)
        return completed(args, stdout=f"{remote_sha} refs/heads/main\n".encode())

    monkeypatch.setattr(environment.subprocess, "run", run_remote)
    provider._spawn_remote_refresh([raw_repo])
    provider._spawn_remote_refresh([raw_repo])

    data = json.loads(cache_path.read_text(encoding="utf-8"))
    assert data["version"] == 1
    assert data["entries"]["existing"] == {"sha": "old"}
    assert data["entries"][raw_repo]["sha"] == remote_sha
    assert calls == [["git", "-C", raw_repo, "ls-remote", "--heads", "origin", "main"]]


def test_local_probe_keeps_usable_section_when_windows_probe_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = EnvironmentInspectionProvider(make_config(tmp_path))

    def fail_windows_probe() -> dict[str, object]:
        raise RuntimeError("windows probe failed")

    monkeypatch.setattr(environment.os, "name", "nt")
    monkeypatch.setattr(provider, "_run_windows_probe", fail_windows_probe)
    monkeypatch.setattr(provider, "_run_lifecycle_probe", lambda: {"synchronization_ok": True})

    payload = provider._run_local_probe()

    assert payload["synchronization_ok"] is True
    assert payload["_probe_section_errors"] == {"windows": "windows probe failed"}


def test_local_probe_raises_when_all_sections_fail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = EnvironmentInspectionProvider(make_config(tmp_path))

    def fail_lifecycle_probe() -> dict[str, object]:
        raise RuntimeError("lifecycle probe failed")

    monkeypatch.setattr(environment.os, "name", "posix")
    monkeypatch.setattr(provider, "_run_lifecycle_probe", fail_lifecycle_probe)

    with pytest.raises(RuntimeError, match="environment probe sections failed"):
        provider._run_local_probe()


@pytest.mark.asyncio
async def test_environment_provider_retains_last_snapshot_when_refresh_fails(
    tmp_path: Path,
) -> None:
    calls = 0

    def probe() -> dict[str, object]:
        nonlocal calls
        calls += 1
        if calls == 1:
            return {"docker_available": False}
        raise RuntimeError("probe unavailable")

    provider = EnvironmentInspectionProvider(make_config(tmp_path), probe_runner=probe)
    first = await provider.refresh()
    second = await provider.refresh(force=True)

    assert second is first
    assert calls == 2
