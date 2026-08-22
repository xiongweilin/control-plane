from __future__ import annotations

import ast
import asyncio
from pathlib import Path

import pytest

from control_plane.portable_authority import PortableRuntimeAuthority
from control_plane.service import _LegacyRoutingBoundary
from portable_runtime.core.capabilities import CapabilityRequest
from portable_runtime.core.registry import ProviderRegistry
from portable_runtime.core.runtime import Runtime
from portable_runtime.stores.memory import InMemoryStateStore


def test_legacy_service_source_has_no_provider_invoke_call() -> None:
    """Provider invocation must remain owned by portable RealityBoundary."""

    source = Path("src/control_plane/service.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    direct_calls: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr != "invoke":
            continue
        value = node.func.value
        if isinstance(value, ast.Name) and value.id == "provider":
            direct_calls.append(node.lineno)
    assert direct_calls == []


def test_legacy_routing_boundary_is_unavailable_without_invoking_provider() -> None:
    class Provider:
        calls = 0

        @property
        def descriptor(self):
            from portable_runtime.core.capabilities import ProviderDescriptor

            return ProviderDescriptor(
                id="legacy-test-provider",
                name="legacy test provider",
                version="1",
                capabilities=["code.edit"],
            )

        async def health(self):
            from portable_runtime.core.capabilities import ProviderHealth

            return ProviderHealth(provider_id=self.descriptor.id, available=True)

        async def invoke(self, request, context):
            self.calls += 1
            raise AssertionError("disabled legacy boundary must not invoke providers")

    registry = ProviderRegistry()
    provider = Provider()
    registry.register(provider)
    boundary = _LegacyRoutingBoundary(registry)
    result = asyncio.run(
        boundary.execute(
            CapabilityRequest(
                id="req-legacy-disabled",
                capability="code.edit",
                instruction="edit",
                resource_ref="repo:test",
                actor_ref="control-plane:compatibility",
                effect_class="write-local",
            )
        )
    )
    assert result.status == "unavailable"
    assert result.error and result.error["code"] == "LegacyRoutingDisabled"
    assert provider.calls == 0


@pytest.mark.asyncio
async def test_code_edit_grant_derives_from_one_standing_owner_policy(tmp_path: Path) -> None:
    async def resolve_version(repo: str) -> str:
        return "abc123"

    authority = PortableRuntimeAuthority(
        Runtime(store=InMemoryStateStore(), registry=ProviderRegistry()),
        version_resolver=resolve_version,
    )
    await authority.prepare_code_edit(
        repair_id="standing-policy-one",
        repo=str(tmp_path),
        prompt="prepare candidate one",
    )
    await authority.prepare_code_edit(
        repair_id="standing-policy-two",
        repo=str(tmp_path),
        prompt="prepare candidate two",
    )

    standing = authority.runtime.store.get_decision("decision_personal_owner_code_edit_v1")
    assert standing is not None
    assert standing.decision_type == "standing-owner-delegation"
    assert standing.authorized_by == ["human:personal-owner"]
    assert standing.metadata["provenance_kind"] == "standing-owner-policy"
    assert standing.metadata["allowed_capabilities"] == ["code.edit"]
    assert standing.metadata["revocable"] is True
    assert authority.runtime.store.get_decision("decision_standing-policy-one_abc123") is None
    assert authority.runtime.store.get_decision("decision_standing-policy-two_abc123") is None
    grants = authority.runtime.store.list_authorizations()
    assert len(grants) == 2
    assert {grant.source_decision_ref for grant in grants} == {standing.id}
    assert all(
        grant.metadata.get("standing_policy_ref") == standing.id
        and grant.metadata.get("provenance") == "derived-from-standing-owner-delegation"
        for grant in grants
    )
