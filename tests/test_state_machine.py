from __future__ import annotations

import pytest

from control_plane.state_machine import RepairState, StateMachineError, require_transition


def test_legal_chain() -> None:
    pairs = [
        (RepairState.QUEUED, RepairState.DIAGNOSING),
        (RepairState.DIAGNOSING, RepairState.PROPOSING),
        (RepairState.PROPOSING, RepairState.NEEDS_APPROVAL),
        (RepairState.NEEDS_APPROVAL, RepairState.APPLYING),
        (RepairState.APPLYING, RepairState.VERIFIED),
        (RepairState.VERIFIED, RepairState.CLOSED),
    ]
    for current, target in pairs:
        require_transition(current, target)


def test_illegal_transition_raises() -> None:
    with pytest.raises(StateMachineError):
        require_transition(RepairState.CLOSED, RepairState.DIAGNOSING)
    with pytest.raises(StateMachineError):
        require_transition(RepairState.QUEUED, RepairState.VERIFIED)


def test_timeout_and_recovery_transitions() -> None:
    require_transition(RepairState.PROPOSING, RepairState.TIMED_OUT)
    require_transition(RepairState.APPLYING, RepairState.TIMED_OUT)
    require_transition(RepairState.NEEDS_APPROVAL, RepairState.RECOVERING)
    require_transition(RepairState.RECOVERING, RepairState.NEEDS_APPROVAL)
    require_transition(RepairState.RECOVERING, RepairState.APPLYING)
    require_transition(RepairState.RECOVERING, RepairState.ROLLED_BACK)
    require_transition(RepairState.RECOVERING, RepairState.CLOSED)
    require_transition(RepairState.NEEDS_APPROVAL, RepairState.ESCALATED)
    require_transition(RepairState.INTERRUPTED, RepairState.RECOVERING)


def test_timed_out_is_terminal() -> None:
    from control_plane.state_machine import TERMINAL_STATES

    assert RepairState.TIMED_OUT in TERMINAL_STATES
    assert RepairState.RECOVERING not in TERMINAL_STATES
