from __future__ import annotations

from dataclasses import replace

from control_plane.approvals import ApprovalManager
from control_plane.budget import Budget
from control_plane.config import ControlPlaneConfig
from control_plane.service import RepairService
from control_plane.state_machine import RepairState
from control_plane.storage import Store


def test_failure_status_exec_timeout_without_commit_is_timed_out() -> None:
    error = "Codex agent timed out without a committed candidate [timeout_kind=exec]"
    assert RepairService._failure_status_for(error) is RepairState.TIMED_OUT


def test_failure_status_plain_failure_is_failed() -> None:
    assert RepairService._failure_status_for("git push failed") is RepairState.FAILED


def test_failure_status_verify_timeout_is_failed() -> None:
    error = "Verification timed out after 60s [timeout_kind=verify]"
    assert RepairService._failure_status_for(error) is RepairState.FAILED


def test_failure_status_comm_timeout_is_failed() -> None:
    error = "Command timed out after 30s [timeout_kind=comm]"
    assert RepairService._failure_status_for(error) is RepairState.FAILED


def test_failure_status_committed_candidate_marker_keeps_failed() -> None:
    # The "without a committed candidate" marker is required: a timeout that
    # produced a commit takes the approval path and never becomes TIMED_OUT.
    error = "Codex agent timed out [timeout_kind=exec]"  # no candidate marker
    assert RepairService._failure_status_for(error) is RepairState.FAILED


def _service(tmp_path) -> RepairService:
    config = replace(
        ControlPlaneConfig(),
        api_key="x",
        data_dir=tmp_path / "data",
        patch_dir=tmp_path / "data" / "patches",
        evidence_dir=tmp_path / "data" / "evidence",
        state_db=tmp_path / "data" / "control-plane.db",
        notification_enabled=False,
        prometheus_url="",
        alertmanager_url="",
    )
    store = Store(config.state_db)
    return RepairService(
        config,
        store,
        Budget(store, 10, 8),
        object(),
        ApprovalManager(),
        object(),
    )


def test_fingerprint_dedup_excludes_interrupted(tmp_path) -> None:
    """An interrupted repair is not a 'finished' repair for dedup/cooldown."""
    service = _service(tmp_path)
    store = service.store
    store.create_repair("repair-1", "fp-1", "{}")
    store.set_repair_status(
        "repair-1",
        RepairState.INTERRUPTED.value,
        finished_at=1_000,
    )
    assert service._latest_finished_repair("fp-1") is None
    store.set_repair_status(
        "repair-1",
        RepairState.FAILED.value,
        finished_at=2_000,
    )
    assert service._latest_finished_repair("fp-1") is not None
    store.close()
