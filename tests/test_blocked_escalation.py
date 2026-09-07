from portable_runtime.controller import (
    CognitiveController,
    ControllerDecision,
    ControllerDecisionKind,
    ControllerStatus,
)
from portable_runtime.core.models import Event, new_id
from portable_runtime.core.runtime import Runtime

from control_plane.alert_policy import ManualTaskPolicy
from control_plane.kernel_bridge import PersonalKernelBridge


async def test_failed_diagnosis_waits_without_closure_or_work() -> None:
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
    assert waiting.active_closure_ref is None
    assert waiting.work_proposal_ref is None
    assert runtime.list_work() == []
