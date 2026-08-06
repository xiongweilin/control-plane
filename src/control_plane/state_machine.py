from __future__ import annotations

from enum import StrEnum


class RepairState(StrEnum):
    QUEUED = "queued"
    DIAGNOSING = "diagnosing"
    PROPOSING = "proposing"
    STAGED = "staged"
    APPLYING = "applying"
    VERIFIED = "verified"
    CLOSED = "closed"
    NEEDS_APPROVAL = "needs_approval"
    ROLLED_BACK = "rolled_back"
    FAILED = "failed"
    ESCALATED = "escalated"
    INTERRUPTED = "interrupted"


_TRANSITIONS: dict[RepairState, frozenset[RepairState]] = {
    RepairState.QUEUED: frozenset({RepairState.DIAGNOSING, RepairState.INTERRUPTED, RepairState.FAILED}),
    RepairState.DIAGNOSING: frozenset({RepairState.PROPOSING, RepairState.INTERRUPTED, RepairState.FAILED}),
    RepairState.PROPOSING: frozenset(
        {
            RepairState.STAGED,
            RepairState.NEEDS_APPROVAL,
            RepairState.VERIFIED,
            RepairState.INTERRUPTED,
            RepairState.FAILED,
        }
    ),
    RepairState.STAGED: frozenset(
        {
            RepairState.APPLYING,
            RepairState.NEEDS_APPROVAL,
            RepairState.INTERRUPTED,
            RepairState.FAILED,
        }
    ),
    RepairState.NEEDS_APPROVAL: frozenset(
        {
            RepairState.STAGED,
            RepairState.APPLYING,
            RepairState.CLOSED,
            RepairState.ROLLED_BACK,
            RepairState.INTERRUPTED,
        }
    ),
    RepairState.APPLYING: frozenset(
        {
            RepairState.VERIFIED,
            RepairState.ROLLED_BACK,
            RepairState.FAILED,
            RepairState.ESCALATED,
            RepairState.INTERRUPTED,
        }
    ),
    RepairState.VERIFIED: frozenset({RepairState.CLOSED, RepairState.INTERRUPTED}),
}

TERMINAL_STATES = frozenset(
    {
        RepairState.CLOSED,
        RepairState.ROLLED_BACK,
        RepairState.FAILED,
        RepairState.ESCALATED,
        RepairState.INTERRUPTED,
    }
)


class StateMachineError(RuntimeError):
    pass


def require_transition(current: RepairState, target: RepairState) -> None:
    allowed = _TRANSITIONS.get(current, frozenset())
    if target not in allowed:
        raise StateMachineError(f"Illegal repair transition: {current.value} -> {target.value}")
