from __future__ import annotations

from pathlib import Path

import pytest

from control_plane.config import ControlPlaneConfig
from control_plane.personal_operations import PersonalOperationsProvider
from control_plane.portable_authority import PortableRuntimeAuthority
from portable_runtime.core.capability_contract import CapabilityContract
from portable_runtime.core.registry import ProviderRegistry
from portable_runtime.core.runtime import Runtime
from portable_runtime.stores.memory import InMemoryStateStore


class Executor:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    async def run(self, args, *, cwd=None, timeout=60, input_text=None, env=None):
        self.calls.append(list(args))
        if "rev-parse" in args:
            return "abc123"
        return ""


def _authority(tmp_path: Path) -> PortableRuntimeAuthority:
    executor = Executor()
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
    runtime.registry.register(PersonalOperationsProvider(ControlPlaneConfig(), executor))  # type: ignore[arg-type]

    async def resolve_version(repo: str) -> str:
        return "abc123"

    return PortableRuntimeAuthority(runtime, version_resolver=resolve_version)


@pytest.mark.asyncio
async def test_human_approval_is_canonical_scoped_and_idempotent(tmp_path: Path) -> None:
    authority = _authority(tmp_path)
    await authority.prepare_code_edit(
        repair_id="repair-human-provenance",
        repo=str(tmp_path),
        prompt="prepare candidate",
    )
    specs = [
        {
            "capability": "git.merge",
            "resource_ref": f"repo:{tmp_path.resolve()}",
            "subject_version_refs": ["git:abc123"],
            "effect_class": "write-local",
        },
        {
            "capability": "git.push",
            "resource_ref": f"repo:{tmp_path.resolve()}",
            "subject_version_refs": ["git:abc123"],
            "effect_class": "write-remote",
        },
    ]

    decision, grants = authority.record_human_approval(
        "repair-human-provenance",
        decided_by="alice",
        note="approved candidate",
        operation_specs=specs,
    )
    decision2, grants2 = authority.record_human_approval(
        "repair-human-provenance",
        decided_by="alice",
        note="approved candidate",
        operation_specs=specs,
    )

    assert decision.id == decision2.id
    assert decision.decision_type == "human-approval"
    assert decision.selected_option == "approve"
    assert {grant.id for grant in grants} == {grant.id for grant in grants2}
    assert {grant.allowed_capabilities[0] for grant in grants} == {"git.merge", "git.push"}
    assert all(grant.source_decision_ref == decision.id for grant in grants)
    assert all(grant.principal_ref == "human:alice" for grant in grants)
    assert all(grant.resource_scope == [f"repo:{tmp_path.resolve()}"] for grant in grants)
    assert all(grant.subject_version_refs == ["git:abc123"] for grant in grants)

    work = authority.runtime.store.get_work("work_legacy_repair-human-provenance")
    assert work is not None
    assert work.metadata["human_approval_decision_ref"] == decision.id
    assert set(work.metadata["human_approval_grant_refs"]) == {grant.id for grant in grants}


@pytest.mark.asyncio
async def test_conflicting_human_approval_is_rejected(tmp_path: Path) -> None:
    authority = _authority(tmp_path)
    await authority.prepare_code_edit(
        repair_id="repair-human-conflict",
        repo=str(tmp_path),
        prompt="prepare candidate",
    )
    spec = {
        "capability": "git.merge",
        "resource_ref": f"repo:{tmp_path.resolve()}",
        "subject_version_refs": ["git:abc123"],
        "effect_class": "write-local",
    }
    authority.record_human_approval(
        "repair-human-conflict",
        decided_by="alice",
        operation_specs=[spec],
    )

    with pytest.raises(RuntimeError, match="conflicting human approval"):
        authority.record_human_approval(
            "repair-human-conflict",
            decided_by="bob",
            operation_specs=[spec],
        )


@pytest.mark.asyncio
async def test_rollback_decision_materializes_explicit_rollback_grant(tmp_path: Path) -> None:
    authority = _authority(tmp_path)
    await authority.prepare_code_edit(
        repair_id="repair-human-rollback",
        repo=str(tmp_path),
        prompt="prepare candidate",
    )
    decision, grants = authority.record_human_approval(
        "repair-human-rollback",
        decided_by="alice",
        action="rollback",
        operation_specs=[
            {
                "capability": "git.rollback",
                "resource_ref": f"repo:{tmp_path.resolve()}",
                "subject_version_refs": ["git:abc123"],
                "effect_class": "write-local",
            }
        ],
    )
    assert decision.selected_option == "rollback"
    assert [grant.allowed_capabilities for grant in grants] == [["git.rollback"]]
    assert all(grant.source_decision_ref == decision.id for grant in grants)


@pytest.mark.asyncio
async def test_reject_decision_materializes_no_effect_grant(tmp_path: Path) -> None:
    authority = _authority(tmp_path)
    await authority.prepare_code_edit(
        repair_id="repair-human-reject",
        repo=str(tmp_path),
        prompt="prepare candidate",
    )
    decision, grants = authority.record_human_approval(
        "repair-human-reject",
        decided_by="alice",
        action="reject",
        operation_specs=[
            {
                "capability": "git.rollback",
                "resource_ref": f"repo:{tmp_path.resolve()}",
                "subject_version_refs": ["git:abc123"],
                "effect_class": "write-local",
            }
        ],
    )
    assert decision.selected_option == "reject"
    assert grants == []
