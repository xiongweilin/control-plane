from __future__ import annotations

from control_plane.service import RepairService
from control_plane.state_machine import RepairState


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
