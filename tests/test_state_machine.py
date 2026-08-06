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
