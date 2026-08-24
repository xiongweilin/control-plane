from __future__ import annotations

import json
from dataclasses import replace

import pytest

from control_plane.approvals import ApprovalManager
from control_plane.budget import Budget
from control_plane.config import ControlPlaneConfig
from control_plane.notify import Notifier
from control_plane.repair_resolution import ResolutionKind, RestorationStatus
from control_plane.service import RepairRejectedError, RepairService
from control_plane.storage import Store
from portable_runtime.core.registry import ProviderRegistry
from portable_runtime.core.runtime import Runtime
from portable_runtime.stores.memory import InMemoryStateStore


@pytest.mark.asyncio
async def test_service_reject_closes_only_through_canonical_decision_and_closure_authority(
    tmp_path,
) -> None:
    config = replace(
        ControlPlaneConfig(),
        run_id="run-c3-reject-wiring",
        data_dir=tmp_path / "data",
        patch_dir=tmp_path / "data" / "patches",
        evidence_dir=tmp_path / "data" / "evidence",
        state_db=tmp_path / "data" / "control-plane.db",
        notification_enabled=False,
        model_preflight_enabled=False,
    )
    store = Store(config.state_db)
    repair_id = "repair-c3-service-reject"
    store.create_repair(repair_id, "fp-c3-service-reject", "{}")
    store.set_repair_status(repair_id, "needs_approval")
    store.add_approval(
        "approval-c3-service-reject",
        "repair",
        repair_id,
        "reject",
        "alice",
        "reject candidate",
    )

    runtime = Runtime(store=InMemoryStateStore(), registry=ProviderRegistry())
    service = RepairService(
        config,
        store,
        Budget(store, 100, 8),
        approvals=ApprovalManager(),
        notifier=Notifier(config),
        portable_runtime=runtime,
    )

    try:
        with pytest.raises(RepairRejectedError):
            await service._apply_approval_decision(repair_id, "reject")

        row = store.get_repair(repair_id)
        assert row is not None
        assert row["status"] == "closed"
        assert row["resolution_kind"] == ResolutionKind.REJECTED.value
        assert row["restoration_status"] == RestorationStatus.UNVERIFIED.value
        assert json.loads(row["restoration_proof_refs_json"]) == []

        basis_refs = json.loads(row["resolution_basis_refs_json"])
        assert len(basis_refs) == 1
        decision = runtime.store.get_decision(basis_refs[0])
        assert decision is not None
        assert decision.decision_type == "human-approval"
        assert decision.selected_option == "reject"
        assert decision.authorized_by == ["human:owner"]
        assert decision.metadata["repair_id"] == repair_id

        work = runtime.store.get_work(f"work_legacy_{repair_id}")
        run = runtime.store.get_run(f"run_legacy_{repair_id}")
        assert work is not None and run is not None
        assert work.metadata["human_approval_decision_ref"] == decision.id
        assert run.metadata["human_approval_decision_ref"] == decision.id
        assert work.metadata["human_approval_action"] == "reject"
        assert run.metadata["human_approval_action"] == "reject"
    finally:
        await service.close()
        store.close()
