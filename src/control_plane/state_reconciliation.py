"""Repair durable control-plane state that cannot represent live execution."""

from __future__ import annotations

from collections import Counter
from typing import Any

from portable_runtime.core.models import Event, new_id, utcnow

_REPAIR_KIND = "personal-incident-repair"
# Legacy journal compatibility only. New failed/invalid diagnosis paths never
# create this Work kind; historical records remain queryable and recoverable.
_BLOCKED_REPAIR_KIND = "personal-incident-repair-blocked"
_RECONCILIATION_EVENT = "ControlPlaneRepairStateReconciled"


def _record_reconciliation(
    runtime: Any,
    *,
    reason: str,
    counts: Counter[str],
    **extra: Any,
) -> None:
    if not counts:
        return
    runtime.store.append_event(
        Event(
            id=new_id("event"),
            type=_RECONCILIATION_EVENT,
            subject_ref=runtime.runtime_id,
            payload={
                "reason": reason,
                "counts": dict(counts),
                **extra,
            },
        )
    )


def reconcile_repair_state(runtime: Any, *, stale_after_seconds: float) -> dict[str, int]:
    """Remove stale execution claims without claiming unverified completion.

    A blocked escalation is an owner-waiting Work, never an active execution.
    A normal repair whose Work and Run have both remained ``running`` beyond
    the bounded repair timeout is interrupted and returned to ``waiting``.
    This deliberately does not mark anything completed: completion requires
    the kernel's proof-bearing completion path.
    """

    now = utcnow()
    counts: Counter[str] = Counter()
    for work in runtime.store.list_work():
        if work.kind not in {_REPAIR_KIND, _BLOCKED_REPAIR_KIND}:
            continue

        is_blocked_kind = work.kind == _BLOCKED_REPAIR_KIND
        is_stale_running = (
            work.status == "running"
            and (now - work.updated_at).total_seconds() >= stale_after_seconds
        )
        if is_blocked_kind and work.status in {"open", "ready", "running"}:
            updated = work.model_copy(update={"status": "waiting", "updated_at": now})
            runtime.store.save_work(updated)
            counts["blocked_work_waiting"] += 1
        elif is_stale_running:
            updated = work.model_copy(update={"status": "waiting", "updated_at": now})
            runtime.store.save_work(updated)
            counts["stale_work_waiting"] += 1

        for run in runtime.store.list_runs(work.id):
            if run.status != "running":
                continue
            if not (is_blocked_kind or is_stale_running):
                continue
            runtime.store.save_run(
                run.model_copy(update={"status": "interrupted", "ended_at": now})
            )
            counts["running_runs_interrupted"] += 1

    if counts:
        _record_reconciliation(
            runtime,
            reason="remove stale repair execution claims without completion proof",
            counts=counts,
            stale_after_seconds=stale_after_seconds,
        )
    return dict(counts)


def settle_waiting_execution_claims(runtime: Any, work: Any | None) -> dict[str, int]:
    """End execution claims after a personal controller reaches WAITING.

    The controller's waiting state is not proof of Work completion. Once the
    bounded policy has returned, leave the Work recoverable and interrupt any
    Run that was only the policy's execution claim. This belongs to the
    control-plane profile; it does not alter Agent Kernel's execution kernel.
    """

    if work is None:
        return {}
    current_work = runtime.get_work(work.id)
    if current_work is None or current_work.status not in {"running", "waiting"}:
        return {}

    now = utcnow()
    counts: Counter[str] = Counter()
    if current_work.status == "running":
        runtime.store.save_work(
            current_work.model_copy(update={"status": "waiting", "updated_at": now})
        )
        counts["waiting_work_settled"] += 1

    for run in runtime.store.list_runs(current_work.id):
        if run.status != "running":
            continue
        runtime.store.save_run(
            run.model_copy(update={"status": "interrupted", "ended_at": now})
        )
        counts["running_runs_interrupted"] += 1

    _record_reconciliation(
        runtime,
        reason="personal controller reached waiting without completion proof",
        counts=counts,
    )
    return dict(counts)


__all__ = ["reconcile_repair_state", "settle_waiting_execution_claims"]
