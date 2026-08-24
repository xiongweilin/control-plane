from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from control_plane.closure_authority import ClosureAuthority, ClosureAuthorityError
from control_plane.repair_resolution import ResolutionKind, RestorationStatus
from control_plane.storage import Store
from portable_runtime.core.models import Decision, Run, Work
from portable_runtime.records.models import EvidenceArtifact
from portable_runtime.stores.memory import InMemoryStateStore
from portable_runtime.workflows.completion import CompletionAuthority


def _terminal_pair(portable: InMemoryStateStore, repair_id: str):
    work = Work(
        id=f"work_legacy_{repair_id}",
        title="repair",
        acceptance_criteria=["verify.objective"],
        metadata={"verification_summary": "objective verified"},
    )
    run = Run(id=f"run_legacy_{repair_id}", work_id=work.id, status="running")
    portable.save_work(work)
    portable.save_run(run)
    proof = EvidenceArtifact(
        id=f"proof_{repair_id}",
        kind="closed-verification",
        lifecycle_status="current",
        metadata={
            "verification_result": {"result": "pass"},
            "work_id": work.id,
            "run_id": run.id,
            "verification_scope": {},
            "work_version": 1,
            "acceptance_criteria": list(work.acceptance_criteria),
            "obligation_refs": CompletionAuthority.required_obligation_refs(work),
        },
    )
    portable.save_record(proof)
    CompletionAuthority(portable).authorize(work=work, run=run, verification_refs=[proof.id])
    return portable.get_work(work.id), portable.get_run(run.id), proof


def _human_pair(portable: InMemoryStateStore, repair_id: str, action: str):
    work = Work(id=f"work_legacy_{repair_id}", title="repair")
    run = Run(id=f"run_legacy_{repair_id}", work_id=work.id, status="waiting")
    portable.save_work(work)
    portable.save_run(run)
    decision = Decision(
        id=f"decision_{repair_id}_human_approval",
        work_id=work.id,
        decision_type="human-approval",
        selected_option=action,
        authorized_by=["human:owner"],
        metadata={"repair_id": repair_id, "source": "test"},
    )
    portable.save_decision(decision)
    return work, run, decision


def test_close_restored_consumes_canonical_terminal_audit_and_preserves_it(tmp_path) -> None:
    repair_id = "restored"
    legacy = Store(tmp_path / "legacy.db")
    legacy.create_repair(repair_id, "fp", "{}")
    portable = InMemoryStateStore()
    work, run, proof = _terminal_pair(portable, repair_id)
    legacy.attach_portable_store(portable, enable_read=True)

    # A legacy workflow projection after canonical completion must not downgrade
    # the terminal Work/Run back to waiting.
    legacy.set_repair_status(repair_id, "verified")
    assert portable.get_work(work.id).status == "completed"
    assert portable.get_run(run.id).status == "succeeded"

    ClosureAuthority(legacy, portable).close_restored(repair_id)
    row = legacy.get_repair(repair_id)
    assert row is not None
    assert row["status"] == "closed"
    assert row["resolution_kind"] == ResolutionKind.RESTORED.value
    assert row["restoration_status"] == RestorationStatus.VERIFIED.value
    assert json.loads(row["restoration_proof_refs_json"]) == [proof.id]
    assert json.loads(row["resolution_basis_refs_json"]) == [work.id, run.id]
    assert portable.get_work(work.id).status == "completed"
    assert portable.get_run(run.id).status == "succeeded"
    legacy.close()


def test_close_restored_rejects_asymmetric_terminal_metadata(tmp_path) -> None:
    repair_id = "tampered"
    legacy = Store(tmp_path / "legacy.db")
    legacy.create_repair(repair_id, "fp", "{}")
    legacy.set_repair_status(repair_id, "verified")
    work = SimpleNamespace(
        id=f"work_legacy_{repair_id}",
        status="completed",
        metadata={
            "_completion_proof_refs": ["proof-a"],
            "completion_required_obligations": ["verify.objective"],
            "completion_covered_obligations": ["verify.objective"],
            "completion_missing_obligations": [],
        },
    )
    run = SimpleNamespace(
        id=f"run_legacy_{repair_id}",
        work_id=work.id,
        status="succeeded",
        metadata={
            "_completion_proof_refs": ["proof-b"],
            "completion_required_obligations": ["verify.objective"],
            "completion_covered_obligations": ["verify.objective"],
            "completion_missing_obligations": [],
        },
    )

    class FakePortable:
        def get_work(self, _):
            return work

        def get_run(self, _):
            return run

    with pytest.raises(ClosureAuthorityError, match="asymmetric"):
        ClosureAuthority(legacy, FakePortable()).close_restored(repair_id)
    row = legacy.get_repair(repair_id)
    assert row["status"] == "verified"
    assert row["resolution_kind"] == ResolutionKind.UNRESOLVED.value
    legacy.close()


def test_rejection_requires_durable_human_decision_and_preserves_restoration(tmp_path) -> None:
    repair_id = "rejected"
    legacy = Store(tmp_path / "legacy.db")
    legacy.create_repair(repair_id, "fp", "{}")
    legacy.set_repair_status(repair_id, "needs_approval")
    portable = InMemoryStateStore()
    work, run, decision = _human_pair(portable, repair_id, "reject")
    legacy.attach_portable_store(portable, enable_read=True)

    ClosureAuthority(legacy, portable).close_rejected(repair_id)
    row = legacy.get_repair(repair_id)
    assert row is not None
    assert row["status"] == "closed"
    assert row["resolution_kind"] == ResolutionKind.REJECTED.value
    assert row["restoration_status"] == RestorationStatus.UNVERIFIED.value
    assert json.loads(row["restoration_proof_refs_json"]) == []
    assert json.loads(row["resolution_basis_refs_json"]) == [decision.id]
    assert portable.get_work(work.id).status != "completed"
    assert portable.get_run(run.id).status != "succeeded"
    legacy.close()


def test_rejection_without_decision_fails_closed(tmp_path) -> None:
    repair_id = "missing-decision"
    legacy = Store(tmp_path / "legacy.db")
    legacy.create_repair(repair_id, "fp", "{}")
    legacy.set_repair_status(repair_id, "needs_approval")
    portable = InMemoryStateStore()
    portable.save_work(Work(id=f"work_legacy_{repair_id}", title="repair"))
    portable.save_run(Run(id=f"run_legacy_{repair_id}", work_id=f"work_legacy_{repair_id}"))

    with pytest.raises(ClosureAuthorityError, match="Decision is missing"):
        ClosureAuthority(legacy, portable).close_rejected(repair_id)
    row = legacy.get_repair(repair_id)
    assert row["status"] == "needs_approval"
    assert row["resolution_kind"] == ResolutionKind.UNRESOLVED.value
    legacy.close()


def test_rollback_records_disposition_but_not_restoration(tmp_path) -> None:
    repair_id = "rolled-back"
    legacy = Store(tmp_path / "legacy.db")
    legacy.create_repair(repair_id, "fp", "{}")
    legacy.set_repair_status(repair_id, "needs_approval")
    portable = InMemoryStateStore()
    _, _, decision = _human_pair(portable, repair_id, "rollback")
    legacy.attach_portable_store(portable, enable_read=True)

    ClosureAuthority(legacy, portable).record_rolled_back(repair_id)
    row = legacy.get_repair(repair_id)
    assert row["status"] == "rolled_back"
    assert row["resolution_kind"] == ResolutionKind.ROLLED_BACK.value
    assert row["restoration_status"] == RestorationStatus.UNVERIFIED.value
    assert json.loads(row["resolution_basis_refs_json"]) == [decision.id]
    legacy.close()


def test_reconcile_restored_projection_is_idempotent_after_cross_store_crash(tmp_path) -> None:
    repair_id = "crash-window"
    legacy = Store(tmp_path / "legacy.db")
    legacy.create_repair(repair_id, "fp", "{}")
    legacy.set_repair_status(repair_id, "applying")
    portable = InMemoryStateStore()
    work, run, proof = _terminal_pair(portable, repair_id)
    legacy.attach_portable_store(portable, enable_read=True)
    authority = ClosureAuthority(legacy, portable)

    assert authority.reconcile_restored_projection(repair_id) is True
    row = legacy.get_repair(repair_id)
    assert row["status"] == "closed"
    assert row["resolution_kind"] == ResolutionKind.RESTORED.value
    assert row["restoration_status"] == RestorationStatus.VERIFIED.value
    assert json.loads(row["restoration_proof_refs_json"]) == [proof.id]
    assert authority.reconcile_restored_projection(repair_id) is False
    assert portable.get_work(work.id).status == "completed"
    assert portable.get_run(run.id).status == "succeeded"
    legacy.close()
