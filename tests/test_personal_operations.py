from pathlib import Path

import pytest
from portable_runtime.core.capabilities import CapabilityRequest, InvocationContext

from control_plane.config import ControlPlaneConfig
from control_plane.personal_operations import PersonalOperationsProvider


def sync_config(tmp_path: Path) -> ControlPlaneConfig:
    return ControlPlaneConfig(
        api_key="test-key",
        automatic_handling_enabled=True,
        owner_principal="principal:test",
        allowed_auto_projects=("test",),
        project_dirs={"test": str(tmp_path)},
        allowed_repo_roots=(str(tmp_path),),
    )


@pytest.mark.asyncio
async def test_exact_push_capability_uses_the_expected_ref_and_900_second_command_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old_sha = "1" * 40
    new_sha = "2" * 40
    provider = PersonalOperationsProvider(sync_config(tmp_path))
    request = CapabilityRequest(
        id="request:exact-push",
        capability="git.push_exact_ref",
        actor_ref="principal:test",
        resource_ref=str(tmp_path),
        effect_class="write-remote",
        parameters={
            "repo": str(tmp_path),
            "project": "test",
            "remote": "origin",
            "branch": "main",
            "expected_old_sha": old_sha,
            "expected_new_sha": new_sha,
        },
    )
    remote_shas = iter((old_sha, new_sha))
    calls = []

    async def fake_run(argv, *, cwd=None, timeout=120):
        calls.append((argv, cwd, timeout))
        if argv[1] == "status":
            return ""
        if argv[1] == "symbolic-ref":
            return "main"
        if argv[1] == "rev-parse":
            return new_sha
        if argv[1] == "ls-remote":
            return f"{next(remote_shas)} refs/heads/main"
        if argv[1] == "merge-base":
            return ""
        if argv[1] == "push":
            return ""
        raise AssertionError(f"unexpected command: {argv}")

    monkeypatch.setattr(provider, "_run", fake_run)
    result = await provider.invoke(request, InvocationContext(runtime_id="test"))

    assert result.status == "succeeded"
    push_calls = [call for call in calls if call[0][1] == "push"]
    assert push_calls == [
        (
            ["git", "push", "origin", f"{new_sha}:refs/heads/main"],
            str(tmp_path),
            900,
        )
    ]


@pytest.mark.asyncio
async def test_unsupported_git_sync_alias_is_not_routed_as_an_effect(
    tmp_path: Path,
) -> None:
    provider = PersonalOperationsProvider(sync_config(tmp_path))
    result = await provider.invoke(
        CapabilityRequest(
            id="request:sync-alias",
            capability="git.sync",
            resource_ref=str(tmp_path),
            parameters={"repo": str(tmp_path)},
        ),
        InvocationContext(runtime_id="test"),
    )

    assert result.status == "failed"
    assert "unsupported git capability" in (result.message or "")


def test_provider_advertises_only_the_exact_synchronization_capability_names(
    tmp_path: Path,
) -> None:
    provider = PersonalOperationsProvider(sync_config(tmp_path))

    assert {
        "git.fast_forward",
        "git.push_exact_ref",
        "chezmoi.apply",
    } <= set(provider.descriptor.capabilities)
    assert "git.sync" not in provider.descriptor.capabilities


@pytest.mark.asyncio
async def test_known_garbage_is_moved_to_reversible_quarantine(tmp_path: Path) -> None:
    source = tmp_path / "portable-runtime-worktrees"
    source.mkdir()
    (source / "generated.txt").write_text("generated", encoding="utf-8")
    quarantine = tmp_path / "quarantine"
    provider = PersonalOperationsProvider(
        ControlPlaneConfig(
            api_key="test-key",
            known_garbage_paths=(str(source),),
            garbage_quarantine_dir=str(quarantine),
            automatic_handling_enabled=True,
        )
    )

    result = await provider.invoke(
        CapabilityRequest(
            id="request:garbage",
            capability="maintenance.cleanup_known_garbage",
            effect_class="write-local",
        ),
        InvocationContext(runtime_id="test"),
    )

    assert result.status == "succeeded"
    assert not source.exists()
    assert sorted(path.name for path in quarantine.iterdir())
