from portable_runtime.controller import (
    CognitiveController,
    ControllerDecision,
    ControllerDecisionKind,
    ControllerState,
)
from portable_runtime.core.models import Event, new_id
from portable_runtime.core.runtime import Runtime

from control_plane.alert_policy import AutonomousRepairPolicy, ManualTaskPolicy

DIAGNOSIS_MODEL = "codex/gpt-5.6-luna"
EXECUTION_MODEL = "codex/gpt-5.6-luna"


def _controller() -> CognitiveController:
    return CognitiveController(Runtime())


def _record(
    controller: CognitiveController,
    state: ControllerState,
    decision: ControllerDecision,
    *,
    status: str = "succeeded",
    message: str = "ok",
    metadata: dict[str, object] | None = None,
) -> None:
    controller.store.append_event(
        Event(
            id=new_id("event"),
            type="ControllerDecisionSelected",
            subject_ref=state.id,
            payload={"decision": decision.model_dump(mode="json")},
        )
    )
    controller.store.append_event(
        Event(
            id=new_id("event"),
            type="ControllerCapabilityResultObserved",
            subject_ref=state.id,
            payload={
                "decision_ref": decision.id,
                "result": {
                    "request_id": new_id("request"),
                    "provider_id": "test",
                    "status": status,
                    "message": message,
                    "metadata": dict(metadata or {}),
                },
            },
        )
    )


def _policy(controller: CognitiveController) -> AutonomousRepairPolicy:
    return AutonomousRepairPolicy(
        controller=controller,
        prompt="alert fact",
        diagnosis_model=DIAGNOSIS_MODEL,
        execution_model=EXECUTION_MODEL,
        repo="C:/repo",
        project="test",
        verification_labels={"alertname": "Broken"},
    )


async def test_alert_policy_starts_with_configured_diagnosis_model() -> None:
    controller = _controller()
    state = ControllerState(id="controller-test")
    decision = await _policy(controller).select(state)
    assert decision.kind is ControllerDecisionKind.INVOKE_CAPABILITY
    assert decision.capability == "reason.generate"
    assert decision.parameters["model"] == DIAGNOSIS_MODEL
    assert decision.parameters["phase"] == "diagnosis"


async def test_alert_policy_uses_configured_model_for_execution() -> None:
    controller = _controller()
    state = ControllerState(id="controller-test")
    policy = _policy(controller)
    diagnosis = await policy.select(state)
    _record(controller, state, diagnosis, message="repair plan")
    execution = await policy.select(state)
    assert execution.capability == "shell.exec"
    assert execution.parameters["model"] == EXECUTION_MODEL
    assert execution.parameters["phase"] == "execution"
    assert "repair plan" in (execution.instruction or "")


async def test_alert_policy_waits_after_second_unresolved_verification() -> None:
    controller = _controller()
    state = ControllerState(id="controller-test")
    policy = _policy(controller)

    for attempt in range(2):
        diagnosis = await policy.select(state)
        assert diagnosis.parameters["model"] == DIAGNOSIS_MODEL
        _record(controller, state, diagnosis, message=f"plan {attempt + 1}")

        execution = await policy.select(state)
        assert execution.parameters["model"] == EXECUTION_MODEL
        _record(controller, state, execution, message=f"executed {attempt + 1}")

        apply = await policy.select(state)
        assert apply.capability == "docker.compose.up"
        _record(controller, state, apply, message="applied")

        verify = await policy.select(state)
        assert verify.capability == "monitor.alert.active"
        _record(
            controller,
            state,
            verify,
            message="still active",
            metadata={"active": True},
        )

    final = await policy.select(state)
    assert final.kind is ControllerDecisionKind.WAIT
    assert "two autonomous repair attempts" in final.reason


async def test_alert_policy_closes_when_monitor_confirms_recovery() -> None:
    controller = _controller()
    state = ControllerState(id="controller-test")
    policy = _policy(controller)
    diagnosis = await policy.select(state)
    _record(controller, state, diagnosis, message="plan")
    execution = await policy.select(state)
    _record(controller, state, execution)
    apply = await policy.select(state)
    _record(controller, state, apply)
    verify = await policy.select(state)
    _record(controller, state, verify, metadata={"active": False})
    close = await policy.select(state)
    assert close.kind is ControllerDecisionKind.CLOSE


async def test_manual_task_uses_luna_for_both_phases() -> None:
    controller = _controller()
    state = ControllerState(id="controller-task")
    policy = ManualTaskPolicy(
        controller=controller,
        prompt="fix it",
        diagnosis_model=DIAGNOSIS_MODEL,
        execution_model=EXECUTION_MODEL,
        repo="C:/repo",
    )
    diagnosis = await policy.select(state)
    assert diagnosis.parameters["model"] == DIAGNOSIS_MODEL
    _record(controller, state, diagnosis, message="do x")
    execution = await policy.select(state)
    assert execution.parameters["model"] == EXECUTION_MODEL
    assert execution.capability == "shell.exec"
    assert diagnosis.parameters["model"] == execution.parameters["model"]
