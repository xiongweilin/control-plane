from __future__ import annotations

from pathlib import Path

import pytest

from control_plane.config import ControlPlaneConfig
from control_plane.personal_operations import PersonalOperationsProvider
from control_plane.portable_authority import PortableRuntimeAuthority
from portable_runtime.core.capabilities import CapabilityRequest, InvocationContext
from portable_runtime.core.capability_contract import CapabilityContract
from portable_runtime.core.registry import ProviderRegistry
from portable_runtime.core.runtime import Runtime
from portable_runtime.stores.memory import InMemoryStateStore


class FakeExecutor:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], str | None]] = []

    async def run(self, args, *, cwd=None, timeout=60, input_text=None, env=None):
        self.calls.append((list(args), cwd))
        if args[-2:] == ["rev-parse", "branch"]:
            return "abc123"
        return "ok"


@pytest.mark.asyncio
async def test_personal_git_provider_is_separate_from_codex(tmp_path: Path) -> None:
    executor = FakeExecutor()
    provider = PersonalOperationsProvider(ControlPlaneConfig(), executor)  # type: ignore[arg-type]
    request = CapabilityRequest(
        id="req-git-merge",
        capability="git.merge",
        resource_ref=f"repo:{tmp_path}",
        actor_ref="personal-agent",
        subject_version_refs=["git:abc123"],
        effect_class="write-local",
        parameters={"repo": str(tmp_path), "branch": "branch", "target": "main"},
    )

    result = await provider.invoke(request, InvocationContext(runtime_id="test"))

    assert result.status == "succeeded"
    assert executor.calls == [
        (["git", "-C", str(tmp_path), "checkout", "-q", "main"], None),
        (["git", "-C", str(tmp_path), "merge", "--ff-only", "branch"], None),
    ]
    assert "code.edit" not in provider.descriptor.capabilities


@pytest.mark.asyncio
async def test_personal_docker_provider_rejects_non_allowlisted_project(tmp_path: Path) -> None:
    executor = FakeExecutor()
    config = ControlPlaneConfig(project_dirs={"dify": str(tmp_path)})
    provider = PersonalOperationsProvider(config, executor)  # type: ignore[arg-type]
    request = CapabilityRequest(
        id="req-docker-restart",
        capability="docker.restart",
        resource_ref="compose:not-allowed",
        actor_ref="personal-agent",
        effect_class="write-remote",
        parameters={"project": "not-allowed", "project_dir": str(tmp_path)},
    )

    result = await provider.invoke(request, InvocationContext(runtime_id="test"))

    assert result.status == "failed"
    assert executor.calls == []


@pytest.mark.asyncio
async def test_operation_authority_requires_runtime_contract_and_grant(tmp_path: Path) -> None:
    executor = FakeExecutor()
    config = ControlPlaneConfig()
    provider = PersonalOperationsProvider(config, executor)
    runtime = Runtime(store=InMemoryStateStore(), registry=ProviderRegistry())
    runtime.contract_registry.register(
        CapabilityContract(
            capability="git.merge",
            minimum_impact_class="write-local",
            effect_semantics="idempotent",
            reversibility="reversible",
            authorization_requirement="required",
            minimum_procedure_profile="standard",
            resource_required=True,
            subject_version_required=True,
            blast_radius=1,
            exposure=1,
        )
    )
    runtime.registry.register(provider)

    async def resolve_version(repo: str) -> str:
        return "abc123"

    authority = PortableRuntimeAuthority(runtime, version_resolver=resolve_version)
    await authority.prepare_code_edit(
        repair_id="repair-operation-authority",
        repo=str(tmp_path),
        prompt="prepare candidate",
    )
    result = await authority.invoke_operation(
        repair_id="repair-operation-authority",
        capability="git.merge",
        resource_ref=f"repo:{tmp_path.resolve()}",
        parameters={"repo": str(tmp_path), "branch": "branch", "target": "main"},
        effect_class="write-local",
        subject_version_refs=["git:abc123"],
    )

    assert result.status == "succeeded", result.model_dump()
    assert any(grant.allowed_capabilities == ["git.merge"] for grant in runtime.store.list_authorizations())
