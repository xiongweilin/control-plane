from __future__ import annotations

from enum import StrEnum


class RepairState(StrEnum):
    QUEUED = "queued"
    DIAGNOSING = "diagnosing"
    PROPOSING = "proposing"
    STAGED = "staged"
    APPLYING = "applying"
    VERIFIED = "verified"
    RECOVERING = "recovering"
    TIMED_OUT = "timed_out"
    CLOSED = "closed"
    NEEDS_APPROVAL = "needs_approval"
    ROLLED_BACK = "rolled_back"
    FAILED = "failed"
    ESCALATED = "escalated"
    INTERRUPTED = "interrupted"


_TRANSITIONS: dict[RepairState, frozenset[RepairState]] = {
    RepairState.QUEUED: frozenset(
        {RepairState.DIAGNOSING, RepairState.INTERRUPTED, RepairState.FAILED, RepairState.TIMED_OUT}
    ),
    RepairState.DIAGNOSING: frozenset(
        {RepairState.PROPOSING, RepairState.INTERRUPTED, RepairState.FAILED, RepairState.TIMED_OUT}
    ),
    RepairState.PROPOSING: frozenset(
        {
            RepairState.STAGED,
            RepairState.NEEDS_APPROVAL,
            RepairState.VERIFIED,
            RepairState.INTERRUPTED,
            RepairState.FAILED,
            RepairState.TIMED_OUT,
        }
    ),
    RepairState.STAGED: frozenset(
        {
            RepairState.APPLYING,
            RepairState.NEEDS_APPROVAL,
            RepairState.INTERRUPTED,
            RepairState.FAILED,
            RepairState.TIMED_OUT,
        }
    ),
    RepairState.NEEDS_APPROVAL: frozenset(
        {
            RepairState.RECOVERING,
            RepairState.STAGED,
            RepairState.APPLYING,
            RepairState.CLOSED,
            RepairState.ROLLED_BACK,
            RepairState.INTERRUPTED,
            RepairState.ESCALATED,
        }
    ),
    RepairState.RECOVERING: frozenset(
        {
            RepairState.NEEDS_APPROVAL,
            RepairState.APPLYING,
            RepairState.CLOSED,
            RepairState.ROLLED_BACK,
            RepairState.ESCALATED,
            RepairState.INTERRUPTED,
            RepairState.FAILED,
        }
    ),
    RepairState.APPLYING: frozenset(
        {
            RepairState.VERIFIED,
            RepairState.ROLLED_BACK,
            RepairState.FAILED,
            RepairState.ESCALATED,
            RepairState.INTERRUPTED,
            RepairState.TIMED_OUT,
        }
    ),
    RepairState.VERIFIED: frozenset({RepairState.CLOSED, RepairState.INTERRUPTED}),
    RepairState.INTERRUPTED: frozenset(
        {
            RepairState.RECOVERING,
            RepairState.FAILED,
            RepairState.CLOSED,
        }
    ),
}

TERMINAL_STATES = frozenset(
    {
        RepairState.CLOSED,
        RepairState.ROLLED_BACK,
        RepairState.FAILED,
        RepairState.ESCALATED,
        RepairState.TIMED_OUT,
    }
)
"""Unrecoverable terminal states: no outgoing transitions are defined."""

RECOVERABLE_STATES = frozenset(
    {
        RepairState.INTERRUPTED,
        RepairState.RECOVERING,
        RepairState.NEEDS_APPROVAL,
    }
)
"""Quiescent-but-recoverable states: the repair is not running but may resume."""

QUIESCENT_STATES = TERMINAL_STATES | RECOVERABLE_STATES
"""All non-active states (terminal or recoverable) for liveness accounting."""


class StateMachineError(RuntimeError):
    pass


def require_transition(current: RepairState, target: RepairState) -> None:
    allowed = _TRANSITIONS.get(current, frozenset())
    if target not in allowed:
        raise StateMachineError(f"Illegal repair transition: {current.value} -> {target.value}")
