from __future__ import annotations

from pathlib import Path

import pytest

from control_plane.config import ControlPlaneConfig
from control_plane.personal_operations import PersonalOperationsProvider
from control_plane.portable_authority import PortableRuntimeAuthority
from control_plane.reconciliation import ReconciliationDescriptorStore
from control_plane.tools import ToolError
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


class QueueExecutor:
    def __init__(self, responses) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[list[str], str | None]] = []

    async def run(self, args, *, cwd=None, timeout=60, input_text=None, env=None):
        self.calls.append((list(args), cwd))
        if not self.responses:
            return "ok"
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


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
        (["git", "-C", str(tmp_path), "status", "--short", "--branch"], None),
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
    authority.record_human_approval(
        "repair-operation-authority",
        decided_by="owner",
        operation_specs=[
            {
                "capability": "git.merge",
                "resource_ref": f"repo:{tmp_path.resolve()}",
                "subject_version_refs": ["git:abc123"],
                "effect_class": "write-local",
            }
        ],
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


@pytest.mark.asyncio
async def test_merge_conflict_is_aborted_and_status_is_recorded(tmp_path: Path) -> None:
    executor = QueueExecutor(
        [
            "ok",
            ToolError("Command failed (exit 1): CONFLICT (content): merge conflict"),
            ToolError("Command failed (exit 1): Automatic merge failed"),
            "ok",
            "## main",
        ]
    )
    provider = PersonalOperationsProvider(ControlPlaneConfig(), executor)  # type: ignore[arg-type]
    request = CapabilityRequest(
        id="req-merge-conflict",
        capability="git.merge",
        parameters={"repo": str(tmp_path), "branch": "candidate", "target": "main"},
        effect_class="write-local",
    )

    result = await provider.invoke(request, InvocationContext(runtime_id="test"))

    assert result.status == "failed"
    assert result.metadata["merge_aborted"] is True
    assert result.metadata["merge_status"] == "## main"
    assert any(call[0][-2:] == ["merge", "--abort"] for call in executor.calls)


@pytest.mark.asyncio
async def test_push_reconcile_confirms_remote_ref(tmp_path: Path) -> None:
    executor = QueueExecutor(["abc123", "ok", "abc123\trefs/heads/main", "abc123\trefs/heads/main"])
    provider = PersonalOperationsProvider(ControlPlaneConfig(), executor)  # type: ignore[arg-type]
    request = CapabilityRequest(
        id="req-push-confirm",
        capability="git.push",
        parameters={"repo": str(tmp_path), "remote": "origin", "branch": "main"},
        effect_class="write-remote",
    )

    result = await provider.invoke(request, InvocationContext(runtime_id="test"))
    reconciled = await provider.reconcile(request.id)

    assert result.status == "succeeded"
    assert result.metadata["remote_commit"] == "abc123"
    assert reconciled is not None
    assert reconciled.status == "succeeded"
    assert reconciled.reconciled is True


@pytest.mark.asyncio
async def test_push_timeout_stays_unknown_until_remote_ref_is_observed(tmp_path: Path) -> None:
    executor = QueueExecutor(["abc123", ToolError("Command timed out after 120s"), "abc123\trefs/heads/main"])
    provider = PersonalOperationsProvider(ControlPlaneConfig(github_ssh_fallback=False), executor)  # type: ignore[arg-type]
    request = CapabilityRequest(
        id="req-push-timeout",
        capability="git.push",
        parameters={"repo": str(tmp_path), "remote": "origin", "branch": "main"},
        effect_class="write-remote",
    )

    result = await provider.invoke(request, InvocationContext(runtime_id="test"))
    reconciled = await provider.reconcile(request.id)

    assert result.status == "unknown"
    assert reconciled is not None
    assert reconciled.status == "succeeded"
    assert reconciled.reconciled is True


@pytest.mark.asyncio
async def test_docker_reconcile_rereads_container_health(tmp_path: Path) -> None:
    executor = QueueExecutor([
        "ok",
        "cp-api\tUp 2 minutes (healthy)",
        "cp-api\tUp 2 minutes (unhealthy)",
    ])
    config = ControlPlaneConfig(project_dirs={"dify": str(tmp_path)})
    provider = PersonalOperationsProvider(config, executor)  # type: ignore[arg-type]
    request = CapabilityRequest(
        id="req-docker-health",
        capability="docker.restart",
        parameters={"project": "dify"},
        effect_class="write-remote",
    )

    result = await provider.invoke(request, InvocationContext(runtime_id="test"))
    reconciled = await provider.reconcile(request.id)

    assert result.status == "unknown"
    assert result.metadata["container_status"].startswith("cp-api")
    assert result.metadata["desired_state"] == "running"
    assert result.metadata["desired_state_verified"] is True
    assert result.metadata["event_attribution"] == "unknown"
    assert result.metadata["event_verified"] is False
    assert reconciled is not None
    assert reconciled.status == "unknown"
    assert reconciled.reconciled is True


@pytest.mark.asyncio
async def test_docker_desired_state_does_not_claim_restart_event(tmp_path: Path) -> None:
    executor = QueueExecutor(["ok", "cp-api\tUp 2 minutes (healthy)"])
    config = ControlPlaneConfig(project_dirs={"dify": str(tmp_path)})
    provider = PersonalOperationsProvider(config, executor)  # type: ignore[arg-type]
    request = CapabilityRequest(
        id="req-docker-restart-attribution",
        capability="docker.restart",
        parameters={"project": "dify"},
        effect_class="write-remote",
    )

    result = await provider.invoke(request, InvocationContext(runtime_id="test"))

    assert result.status == "unknown"
    assert result.metadata["desired_state_verified"] is True
    assert result.metadata["event_attribution"] == "unknown"
    assert result.metadata["event_verification_basis"] == "not-observable"


@pytest.mark.asyncio
async def test_docker_restart_reconciliation_keeps_healthy_state_non_terminal(tmp_path: Path) -> None:
    # A healthy postcondition is deliberately insufficient to attribute the
    # restart event.  The durable reconciliation path must remain UNKNOWN
    # until an effect-specific restart identity is observed.
    executor = QueueExecutor(
        [
            "cp-api\tUp 2 minutes (healthy)",  # pre-effect baseline
            "restart output",
            "cp-api\tUp 2 minutes (healthy)",  # immediate post-effect probe
            "cp-api\tUp 2 minutes (healthy)",  # reconciliation probe
        ]
    )
    config = ControlPlaneConfig(project_dirs={"dify": str(tmp_path)})
    store = ReconciliationDescriptorStore(tmp_path / "reconciliation.db")
    provider = PersonalOperationsProvider(config, executor, store)  # type: ignore[arg-type]
    request = CapabilityRequest(
        id="req-docker-restart-healthy-reconcile",
        capability="docker.restart",
        parameters={"project": "dify"},
        effect_class="write-remote",
    )

    result = await provider.invoke(request, InvocationContext(runtime_id="test"))

    assert result.status == "unknown"
    assert result.reconciled is True
    assert result.metadata["desired_state_verified"] is True
    assert result.metadata["event_attribution"] == "unknown"
    assert result.metadata["event_verified"] is False
    assert result.metadata["event_verification_basis"] == "not-observable"
    descriptor = store.get_by_request(request.id)
    assert descriptor is not None
    assert descriptor.state.value == "unknown"
    assert descriptor.last_observation is not None
    assert descriptor.last_observation.verdict.value == "unknown"


@pytest.mark.asyncio
async def test_compose_up_is_reported_as_desired_state_operation(tmp_path: Path) -> None:
    executor = QueueExecutor(["ok", "cp-api\tUp 2 minutes (healthy)"])
    config = ControlPlaneConfig(project_dirs={"dify": str(tmp_path)})
    provider = PersonalOperationsProvider(config, executor)  # type: ignore[arg-type]
    request = CapabilityRequest(
        id="req-docker-compose-up",
        capability="docker.compose.up",
        parameters={"project": "dify"},
        effect_class="write-remote",
    )

    result = await provider.invoke(request, InvocationContext(runtime_id="test"))

    assert result.status == "succeeded"
    assert result.metadata["desired_state"] == "running"
    assert result.metadata["desired_state_verified"] is True
    assert result.metadata["event_attribution"] == "not-applicable"
    assert result.metadata["event_verification_basis"] == "desired-state-operation"
