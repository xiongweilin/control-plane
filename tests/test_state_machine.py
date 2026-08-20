from __future__ import annotations

import pytest

from control_plane.state_machine import (
    _TRANSITIONS,
    QUIESCENT_STATES,
    RECOVERABLE_STATES,
    TERMINAL_STATES,
    RepairState,
    StateMachineError,
    require_transition,
)


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
    assert RepairState.TIMED_OUT in TERMINAL_STATES
    assert RepairState.RECOVERING not in TERMINAL_STATES


def test_interrupted_is_recoverable_not_terminal() -> None:
    # INTERRUPTED was previously in TERMINAL_STATES while also having outgoing
    # transitions (INTERRUPTED -> RECOVERING/FAILED/CLOSED): a self-contradiction.
    # It is a recoverable quiescent state, not a terminal one.
    assert RepairState.INTERRUPTED not in TERMINAL_STATES
    assert RepairState.INTERRUPTED in RECOVERABLE_STATES
    assert RepairState.RECOVERING in RECOVERABLE_STATES
    assert RepairState.NEEDS_APPROVAL in RECOVERABLE_STATES


def test_state_sets_are_partitioned() -> None:
    all_states = set(RepairState)
    assert TERMINAL_STATES.isdisjoint(RECOVERABLE_STATES)
    assert QUIESCENT_STATES == TERMINAL_STATES | RECOVERABLE_STATES
    assert all_states >= QUIESCENT_STATES
    assert RepairState.APPLYING not in QUIESCENT_STATES
    assert RepairState.VERIFIED not in QUIESCENT_STATES


def test_terminal_states_have_no_outgoing_transitions() -> None:
    # The split contract: every state in TERMINAL_STATES must have no defined
    # outgoing transitions (they are unrecoverable). Any state not in
    # TERMINAL_STATES must have a transition table entry.
    defined = set(_TRANSITIONS)
    for state in TERMINAL_STATES:
        assert state not in defined, f"terminal state {state.value} must not have outgoing transitions"
    for state in set(RepairState):
        if state not in TERMINAL_STATES:
            assert state in defined, f"non-terminal state {state.value} must define outgoing transitions"


def test_resume_after_interrupt_is_legal_chain() -> None:
    # "中断后恢复": an interrupted repair can be recovered and still finish.
    chain = [
        (RepairState.QUEUED, RepairState.INTERRUPTED),
        (RepairState.INTERRUPTED, RepairState.RECOVERING),
        (RepairState.RECOVERING, RepairState.NEEDS_APPROVAL),
        (RepairState.NEEDS_APPROVAL, RepairState.APPLYING),
        (RepairState.APPLYING, RepairState.VERIFIED),
        (RepairState.VERIFIED, RepairState.CLOSED),
    ]
    for current, target in chain:
        require_transition(current, target)


def test_interrupted_can_fail_or_close() -> None:
    require_transition(RepairState.INTERRUPTED, RepairState.FAILED)
    require_transition(RepairState.INTERRUPTED, RepairState.CLOSED)


def test_timeout_without_commit_is_terminal() -> None:
    # A timeout without a committed candidate is terminal (no recovery path).
    require_transition(RepairState.PROPOSING, RepairState.TIMED_OUT)
    require_transition(RepairState.STAGED, RepairState.TIMED_OUT)
    with pytest.raises(StateMachineError):
        require_transition(RepairState.TIMED_OUT, RepairState.RECOVERING)
    with pytest.raises(StateMachineError):
        require_transition(RepairState.TIMED_OUT, RepairState.NEEDS_APPROVAL)


def test_timeout_with_committed_candidate_goes_to_approval() -> None:
    # "超时后产生提交": the candidate survives the timeout; the repair becomes
    # NEEDS_APPROVAL (recoverable), never TIMED_OUT.
    require_transition(RepairState.PROPOSING, RepairState.NEEDS_APPROVAL)
    require_transition(RepairState.PROPOSING, RepairState.STAGED)
    require_transition(RepairState.STAGED, RepairState.NEEDS_APPROVAL)
    require_transition(RepairState.NEEDS_APPROVAL, RepairState.RECOVERING)
