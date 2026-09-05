from datetime import timedelta

from portable_runtime.core.models import utcnow
from portable_runtime.core.runtime import Runtime

from control_plane.state_reconciliation import (
    reconcile_repair_state,
    settle_waiting_execution_claims,
)


def test_reconcile_repair_state_removes_impossible_running_claims() -> None:
    runtime = Runtime(runtime_id="personal-platform")
    blocked = runtime.create_work(
        title="blocked",
        kind="personal-incident-repair-blocked",
    )
    blocked_run = runtime.start_run(blocked.id, workflow_id="personal-blocked-escalation")

    stale = runtime.create_work(title="stale", kind="personal-incident-repair")
    stale_run = runtime.start_run(stale.id, workflow_id="generic-task")
    runtime.store.save_work(
        stale.model_copy(
            update={
                "status": "running",
                "updated_at": utcnow() - timedelta(hours=1),
            }
        )
    )

    counts = reconcile_repair_state(runtime, stale_after_seconds=900)

    assert counts == {
        "blocked_work_waiting": 1,
        "running_runs_interrupted": 2,
        "stale_work_waiting": 1,
    }
    assert runtime.get_work(blocked.id).status == "waiting"
    assert runtime.get_work(stale.id).status == "waiting"
    assert runtime.store.get_run(blocked_run.id).status == "interrupted"
    assert runtime.store.get_run(stale_run.id).status == "interrupted"


def test_settle_waiting_execution_claims_does_not_claim_completion() -> None:
    runtime = Runtime(runtime_id="personal-platform")
    work = runtime.create_work(title="waiting", kind="personal-incident-repair")
    run = runtime.start_run(work.id, workflow_id="personal-incident-repair")

    counts = settle_waiting_execution_claims(runtime, work)

    assert counts == {"running_runs_interrupted": 1, "waiting_work_settled": 1}
    assert runtime.get_work(work.id).status == "waiting"
    assert runtime.store.get_run(run.id).status == "interrupted"
