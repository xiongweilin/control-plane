from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from control_plane.approvals import ApprovalManager
from control_plane.budget import Budget
from control_plane.config import ControlPlaneConfig
from control_plane.notify import Notifier
from control_plane.reconciliation import (
    BaselineSnapshot,
    GitPushObservationCoordinates,
    GitPushOperation,
    GitPushPostcondition,
    ReconciliationDescriptor,
    ReconciliationDescriptorStore,
    ReconciliationObservation,
    ReconciliationVerdict,
)
from control_plane.service import RepairService
from control_plane.storage import Store
from portable_runtime.core.capabilities import CapabilityRequest, CapabilityResult


class _FakeExecutor:
    async def run(self, args: list[str], **_: object) -> str:
        return ""


class _FakeCapabilities:
    def __init__(self, store: ReconciliationDescriptorStore, verdict: ReconciliationVerdict) -> None:
        self.store = store
        self.verdict = verdict
        self.calls: list[tuple[str, str]] = []

    async def reconcile(self, request_id: str, provider_id: str) -> CapabilityResult:
        self.calls.append((request_id, provider_id))
        descriptor = self.store.get_by_request(request_id)
        assert descriptor is not None
        self.store.record_observation(
            descriptor.id,
            ReconciliationObservation(
                verdict=self.verdict,
                message=f"observed {self.verdict.value}",
            ),
        )
        status = "succeeded" if self.verdict is ReconciliationVerdict.APPLIED else "unknown"
        return CapabilityResult(
            request_id=request_id,
            provider_id=provider_id,
            status=status,
            message=f"observed {self.verdict.value}",
            reconciled=True,
        )


class _FakeRuntime:
    def __init__(self, capabilities: _FakeCapabilities) -> None:
        self.capabilities = capabilities


def _config(tmp_path: Path) -> ControlPlaneConfig:
    data_dir = tmp_path / "data"
    return replace(
        ControlPlaneConfig(),
        data_dir=data_dir,
        patch_dir=data_dir / "patches",
        evidence_dir=data_dir / "evidence",
        state_db=data_dir / "control-plane.db",
        pid_file=data_dir / "control-plane.pid",
        notification_enabled=False,
        model_preflight_enabled=False,
    )


def _descriptor(repair_id: str, store: ReconciliationDescriptorStore) -> ReconciliationDescriptor:
    request = CapabilityRequest(
        id=f"request-{repair_id}",
        capability="git.push",
        work_id=f"work_legacy_{repair_id}",
        run_id=f"run_legacy_{repair_id}",
        resource_ref="repo:control-plane",
        subject_version_refs=["git:abc123"],
        parameters={"repo": "D:/agent/control-plane"},
    )
    descriptor = ReconciliationDescriptor.from_request(
        descriptor_id=f"recon-{repair_id}",
        request=request,
        provider_id="personal-operations",
        provider_version="1.0.0",
        operation=GitPushOperation(repo="D:/agent/control-plane", expected_commit="abc123"),
        pre_effect_baseline=BaselineSnapshot(values={"remote_commit": "base000"}),
        expected_postcondition=GitPushPostcondition(
            remote="origin", branch="main", expected_commit="abc123"
        ),
        observation_coordinates=GitPushObservationCoordinates(
            repo="D:/agent/control-plane",
            remote="origin",
            branch="main",
            remote_ref="refs/heads/main",
        ),
    )
    store.save(descriptor)
    return descriptor


def _service(config: ControlPlaneConfig, store: Store) -> RepairService:
    http = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200)))
    return RepairService(
        config,
        store,
        Budget(store, 10, 4),
        agent=object(),
        approvals=ApprovalManager(),
        notifier=Notifier(config),
        executor=_FakeExecutor(),
        http=http,
    )


@pytest.mark.asyncio
async def test_startup_reconciliation_applied_verifies_without_approval_waiter(tmp_path: Path) -> None:
    config = _config(tmp_path)
    store = Store(config.state_db)
    repair_id = "repair-applied-startup"
    store.create_repair(repair_id, "fp-startup", "{}", 1)
    store.set_repair_status(repair_id, "recovering", error="provider outcome unknown")
    descriptors = ReconciliationDescriptorStore(tmp_path / "reconciliation.db")
    descriptor = _descriptor(repair_id, descriptors)
    capabilities = _FakeCapabilities(descriptors, ReconciliationVerdict.APPLIED)
    service = _service(config, store)
    service.portable_runtime = _FakeRuntime(capabilities)
    verified: list[str] = []

    async def finish(recovered_repair_id: str) -> None:
        verified.append(recovered_repair_id)

    service._finish_resumed_repair = finish  # type: ignore[method-assign]
    outcomes = await service.reconcile_startup_descriptors(descriptors)

    assert capabilities.calls == [(descriptor.request_id, descriptor.provider_id)]
    assert outcomes[0]["state"] == "applied"
    assert outcomes[0]["next_action"] == "deterministic-verification"
    assert verified == [repair_id]
    assert store.get_repair(repair_id)["status"] == "recovering"
    assert descriptors.list_open() == []
    await service.close()
    descriptors.close()
    store.close()


@pytest.mark.asyncio
async def test_startup_reconciliation_unknown_stays_recovering_and_does_not_wait_for_approval(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    store = Store(config.state_db)
    repair_id = "repair-unknown-startup"
    store.create_repair(repair_id, "fp-startup", "{}", 1)
    store.set_repair_status(repair_id, "recovering", error="provider outcome unknown")
    descriptors = ReconciliationDescriptorStore(tmp_path / "reconciliation.db")
    descriptor = _descriptor(repair_id, descriptors)
    capabilities = _FakeCapabilities(descriptors, ReconciliationVerdict.UNKNOWN)
    service = _service(config, store)
    service.portable_runtime = _FakeRuntime(capabilities)

    outcomes = await service.reconcile_startup_descriptors(descriptors)

    assert outcomes[0]["state"] == "unknown"
    assert outcomes[0]["next_action"] == "observe-or-escalate"
    assert store.get_repair(repair_id)["status"] == "recovering"
    assert [item.id for item in descriptors.list_open()] == [descriptor.id]
    await service.close()
    descriptors.close()
    store.close()


@pytest.mark.asyncio
async def test_startup_reconciliation_not_applied_requires_policy_not_approval(tmp_path: Path) -> None:
    config = _config(tmp_path)
    store = Store(config.state_db)
    repair_id = "repair-not-applied-startup"
    store.create_repair(repair_id, "fp-startup", "{}", 1)
    store.set_repair_status(repair_id, "needs_approval")
    descriptors = ReconciliationDescriptorStore(tmp_path / "reconciliation.db")
    _descriptor(repair_id, descriptors)
    capabilities = _FakeCapabilities(descriptors, ReconciliationVerdict.NOT_APPLIED)
    service = _service(config, store)
    service.portable_runtime = _FakeRuntime(capabilities)

    outcomes = await service.reconcile_startup_descriptors(descriptors)

    assert outcomes[0]["state"] == "not-applied"
    assert outcomes[0]["next_action"] == "reauthorize-or-policy-retry"
    assert store.get_repair(repair_id)["status"] == "recovering"
    assert descriptors.list_open() == []
    await service.close()
    descriptors.close()
    store.close()
