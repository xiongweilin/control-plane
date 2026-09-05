from portable_runtime.controller import (
    CognitiveController,
    ControllerDecision,
    ControllerDecisionKind,
    ControllerStatus,
)
from portable_runtime.core.models import Event, new_id
from portable_runtime.core.runtime import Runtime

from control_plane.alert_policy import ManualTaskPolicy
from control_plane.escalation_policy import create_fail_safe_alert_work, preserve_blocked_wait
from control_plane.kernel_bridge import PersonalKernelBridge


async def test_failed_diagnosis_still_produces_durable_waiting_work_without_effects() -> None:
    runtime = Runtime(runtime_id="personal-platform")
    controller = CognitiveController(runtime)
    bridge = PersonalKernelBridge(runtime, controller, owner_principal="principal:test")
    state, _assessment_ref = bridge.begin(
        title="Personal task",
        description="fix it",
        kind="personal-command",
        repo="C:/repo",
    )
    policy = ManualTaskPolicy(
        controller=controller,
        bridge=bridge,
        prompt="fix it",
        diagnosis_model="gpt-5.6-luna",
        execution_model="gpt-5.6-luna",
        repo="C:/repo",
    )

    diagnosis = await policy.select(state)
    assert diagnosis.kind is ControllerDecisionKind.INVOKE_CAPABILITY
    controller.store.append_event(
        Event(
            id=new_id("event"),
            type="ControllerDecisionSelected",
            subject_ref=state.id,
            payload={"decision": diagnosis.model_dump(mode="json")},
        )
    )
    controller.store.append_event(
        Event(
            id=new_id("event"),
            type="ControllerCapabilityResultObserved",
            subject_ref=state.id,
            payload={
                "decision_ref": diagnosis.id,
                "result": {
                    "request_id": new_id("request"),
                    "provider_id": "test",
                    "status": "failed",
                    "message": "diagnosis unavailable",
                    "metadata": {},
                },
            },
        )
    )

    wait_decision: ControllerDecision = await policy.select(state)
    assert wait_decision.kind is ControllerDecisionKind.WAIT
    waiting = await controller.apply(wait_decision)
    assert waiting.status is ControllerStatus.WAITING
    assert waiting.work_proposal_ref is None
    assert runtime.list_work() == []

    settled = await preserve_blocked_wait(controller, bridge, waiting)
    work = bridge.work_for_state(settled)
    assert settled.status is ControllerStatus.WAITING
    assert work is not None
    assert work.requested_capabilities == []
    assert work.metadata["responsibility_proposal_ref"] == settled.work_proposal_ref

    results = bridge.result_events(settled.id, work_id=work.id)
    assert len(results) == 1
    assert results[0].payload["stage"] == "cognitive-blocker"
    assert results[0].payload["result"]["status"] == "failed"

    revisions = controller.revisions(settled.id)
    assert revisions[-1].work_ref == work.id
    assert revisions[-1].failure_class == "diagnosis-failure"
    assert revisions[-1].recommended_disposition.value == "wait"


async def test_fail_safe_alert_materializes_no_effect_work() -> None:
    runtime = Runtime(runtime_id="personal-platform")
    controller = CognitiveController(runtime)
    bridge = PersonalKernelBridge(runtime, controller, owner_principal="principal:test")

    state = await create_fail_safe_alert_work(
        controller,
        bridge,
        title="Alert (fail-safe): WinDefendStopped",
        description="WinDefend stopped; third-party protection is unknown",
        verification_labels={"alertname": "WinDefendStopped"},
    )
    work = bridge.work_for_state(state)

    assert state.status is ControllerStatus.WAITING
    assert work is not None
    assert work.requested_capabilities == []
    assert work.kind == "personal-incident-repair-blocked"
    assert work.status == "waiting"
    run = runtime.store.list_runs(work.id)[-1]
    assert run.status == "interrupted"
