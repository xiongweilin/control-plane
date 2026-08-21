from __future__ import annotations

from pathlib import Path

import pytest
from dataclasses import replace

from control_plane.budget import Budget
from control_plane.config import ControlPlaneConfig
from control_plane.models import Alert
from control_plane.notify import Notifier
from control_plane.portable_authority import PortableRuntimeAuthority
from control_plane.service import RepairService
from control_plane.storage import Store
from portable_runtime.core.boundary import RealityBoundary
from portable_runtime.core.capabilities import CapabilityRequest, CapabilityResult, ProviderDescriptor, ProviderHealth
from portable_runtime.core.capability_contract import (
    CapabilityContractRegistry,
    EffectContractMissing,
    compute_effective_procedure_profile,
)
from portable_runtime.core.registry import ProviderRegistry
from portable_runtime.core.runtime import Runtime
from portable_runtime.stores.memory import InMemoryStateStore


class FakeCodexProvider:
    def __init__(self) -> None:
        self.calls = []
        self._descriptor = ProviderDescriptor(
            id="codex-test",
            name="Codex test provider",
            version="1",
            capabilities=["code.edit", "reason.generate"],
        )

    @property
    def descriptor(self) -> ProviderDescriptor:
        return self._descriptor

    async def health(self) -> ProviderHealth:
        return ProviderHealth(provider_id=self.descriptor.id, available=True)

    async def invoke(self, request, context) -> CapabilityResult:
        self.calls.append((request, context))
        return CapabilityResult(
            request_id=request.id,
            provider_id=self.descriptor.id,
            status="succeeded",
            message="portable fake edit complete",
        )

    async def cancel(self, request_id: str) -> None:
        return None

    async def reconcile(self, request_id: str) -> CapabilityResult | None:
        return None


class FakeGitExecutor:
    async def run(self, args, *, cwd=None, timeout=60, input_text=None):
        if args[-2:] == ["rev-parse", "HEAD"]:
            return "abc123"
        return ""


@pytest.mark.asyncio
async def test_personal_authority_uses_full_reality_boundary_and_versioned_grant(tmp_path: Path) -> None:
    store = InMemoryStateStore()
    registry = ProviderRegistry()
    provider = FakeCodexProvider()
    registry.register(provider)
    runtime = Runtime(store=store, registry=registry)

    async def resolve_version(repo: str) -> str:
        assert repo == str(tmp_path)
        return "abc123"

    authority = PortableRuntimeAuthority(runtime, version_resolver=resolve_version)
    result = await authority.invoke(
        repair_id="repair-authority-1",
        repo=str(tmp_path),
        prompt="make the local repair",
    )

    assert result.status == "succeeded", result.model_dump()
    assert isinstance(runtime.capabilities.boundary, RealityBoundary)
    assert len(provider.calls) == 1
    request, context = provider.calls[0]
    assert request.capability == "code.edit"
    assert request.actor_ref == "personal-agent"
    assert request.resource_ref == f"repo:{tmp_path.resolve()}"
    assert request.subject_version_refs == ["git:abc123"]
    assert request.effect_class == "write-local"
    assert context.work_id == "work_legacy_repair-authority-1"
    assert runtime.store.get_work("work_legacy_repair-authority-1").status == "waiting"
    assert runtime.store.get_run("run_legacy_repair-authority-1").status == "waiting"
    authority.finalize_repair("repair-authority-1", verified=True, summary="verification passed")
    assert runtime.store.get_work("work_legacy_repair-authority-1").status == "completed"
    assert runtime.store.get_run("run_legacy_repair-authority-1").status == "succeeded"
    grants = runtime.store.list_authorizations()
    assert len(grants) == 1
    assert grants[0].grantee_ref == "personal-agent"
    assert grants[0].subject_version_refs == ["git:abc123"]


def test_procedure_profile_minimum_is_monotonic() -> None:
    assert compute_effective_procedure_profile("standard", "minimal") == "standard"
    assert compute_effective_procedure_profile("minimal", "enhanced") == "enhanced"
    with pytest.raises(ValueError):
        compute_effective_procedure_profile("standard", "unknown")


@pytest.mark.asyncio
async def test_personal_authority_cannot_downgrade_code_edit_procedure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = InMemoryStateStore()
    registry = ProviderRegistry()
    provider = FakeCodexProvider()
    registry.register(provider)
    runtime = Runtime(store=store, registry=registry)

    async def resolve_version(repo: str) -> str:
        return "abc123"

    authority = PortableRuntimeAuthority(runtime, version_resolver=resolve_version)
    work, run, resource, version_ref = await authority.prepare_code_edit(
        repair_id="repair-profile-floor",
        repo=str(tmp_path),
        prompt="make the local repair",
    )

    observed_profiles: list[str] = []
    from portable_runtime.workflows import procedure

    original_check = procedure.check_procedure

    def capture_profile(work_value, run_value, profile, **kwargs):
        observed_profiles.append(str(profile))
        return original_check(work_value, run_value, profile, **kwargs)

    monkeypatch.setattr(procedure, "check_procedure", capture_profile)
    request = CapabilityRequest(
        id="req-profile-floor",
        capability="code.edit",
        work_id=work.id,
        run_id=run.id,
        instruction="make the local repair",
        parameters={"repo": str(tmp_path)},
        resource_ref=resource,
        subject_version_refs=[version_ref],
        actor_ref="personal-agent",
        effect_class="write-local",
        metadata={
            "portable_authority": "personal-runtime",
            "procedure_profile": "minimal",
            "resource_ref": resource,
            "subject_version_refs": [version_ref],
            "actor_ref": "personal-agent",
        },
    )

    result = await runtime.capabilities.invoke(request)

    assert result.status == "succeeded", result.model_dump()
    assert observed_profiles and observed_profiles[0] == "standard"


def test_unknown_code_capability_is_not_read_by_default() -> None:
    registry = CapabilityContractRegistry()
    with pytest.raises(EffectContractMissing):
        registry.resolve("code.delete")
    assert registry.resolve("metrics.read").minimum_impact_class == "read"


@pytest.mark.asyncio
async def test_repair_service_uses_runtime_authority_instead_of_legacy_runner(tmp_path: Path) -> None:
    config = replace(
        ControlPlaneConfig(),
        data_dir=tmp_path / "data",
        patch_dir=tmp_path / "data" / "patches",
        evidence_dir=tmp_path / "data" / "evidence",
        state_db=tmp_path / "data" / "control-plane.db",
        notification_enabled=False,
    )
    store = Store(config.state_db)
    registry = ProviderRegistry()
    provider = FakeCodexProvider()
    registry.register(provider)
    runtime = Runtime(store=InMemoryStateStore(), registry=registry)

    class LegacyRunner:
        calls = 0

        async def run_task(self, **kwargs):
            self.calls += 1

        def cli_info(self):
            return Path("codex"), "legacy"

    legacy = LegacyRunner()
    service = RepairService(
        config,
        store,
        Budget(store, 10, 2),
        legacy,
        Notifier(config),
        executor=FakeGitExecutor(),
        portable_runtime=runtime,
    )
    result = await service._invoke_codex_via_capability(
        repair_id="repair-service-authority",
        repo=str(tmp_path),
        prompt="edit through portable authority",
    )

    assert result.exit_code == 0
    assert legacy.calls == 0
    assert isinstance(service.capability_service.boundary, RealityBoundary)
    assert provider.calls[0][0].actor_ref == "personal-agent"
    await service.close()
    store.close()


@pytest.mark.asyncio
async def test_alert_admission_materialises_canonical_work_before_legacy_row(tmp_path: Path) -> None:
    config = replace(
        ControlPlaneConfig(),
        data_dir=tmp_path / "data",
        patch_dir=tmp_path / "data" / "patches",
        evidence_dir=tmp_path / "data" / "evidence",
        state_db=tmp_path / "data" / "control-plane.db",
        notification_enabled=False,
    )
    store = Store(config.state_db)
    runtime = Runtime(store=InMemoryStateStore(), registry=ProviderRegistry())
    service = RepairService(
        config,
        store,
        Budget(store, 10, 2),
        object(),
        Notifier(config),
        executor=FakeGitExecutor(),
        portable_runtime=runtime,
    )

    async def no_op_repair(*args, **kwargs) -> None:
        return None

    service._run_repair = no_op_repair
    alert = Alert.model_validate(
        {
            "status": "firing",
            "labels": {"alertname": "CanonicalAdmission", "instance": "node1"},
            "annotations": {"summary": "canonical admission test"},
            "startsAt": "2026-08-21T00:00:00Z",
            "endsAt": None,
            "fingerprint": "canonical-admission-fp",
        }
    )
    response = await service._start_repair(alert, 1)

    assert response == {"accepted": 1}
    legacy = store.list_repairs(limit=1)[0]
    work = runtime.get_work(f"work_legacy_{legacy['id']}")
    run = runtime.store.get_run(f"run_legacy_{legacy['id']}")
    assert work is not None and work.metadata["canonical_authority"] == "portable-runtime"
    assert run is not None and run.status == "queued"
    await service.close()
    store.close()
