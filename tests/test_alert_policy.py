from portable_runtime.controller import (
    CognitiveController,
    ControllerDecision,
    ControllerDecisionKind,
    ControllerStatus,
)
from portable_runtime.core.models import Event, new_id
from portable_runtime.core.runtime import Runtime

from control_plane.alert_policy import AutonomousRepairPolicy, ManualTaskPolicy
from control_plane.kernel_bridge import PersonalKernelBridge

DIAGNOSIS_MODEL = "codex/gpt-5.6-luna"
EXECUTION_MODEL = "codex/gpt-5.6-luna"


def _setup(*, kind: str = "personal-command"):
    runtime = Runtime(runtime_id="personal-platform")
    controller = CognitiveController(runtime)
    bridge = PersonalKernelBridge(runtime, controller, owner_principal="principal:test")
    state, assessment_ref = bridge.begin(
        title="Personal task",
        description="fix it",
        kind=kind,
        repo="C:/repo",
        project="test" if kind == "personal-incident-repair" else None,
        verification_labels={"alertname": "Broken"} if kind == "personal-incident-repair" else {},
    )
    return runtime, controller, bridge, state, assessment_ref


def _record_cognitive_result(
    controller: CognitiveController,
    state_id: str,
    decision: ControllerDecision,
    *,
    status: str = "succeeded",
    message: str = "bounded plan",
) -> None:
    controller.store.append_event(
        Event(
            id=new_id("event"),
            type="ControllerDecisionSelected",
            subject_ref=state_id,
            payload={"decision": decision.model_dump(mode="json")},
        )
    )
    controller.store.append_event(
        Event(
            id=new_id("event"),
            type="ControllerCapabilityResultObserved",
            subject_ref=state_id,
            payload={
                "decision_ref": decision.id,
                "result": {
                    "request_id": new_id("request"),
                    "provider_id": "test",
                    "status": status,
                    "message": message,
                    "metadata": {},
                },
            },
        )
    )


async def test_manual_task_requires_closure_and_work_proposal_before_work_exists() -> None:
    runtime, controller, bridge, state, assessment_ref = _setup()
    policy = ManualTaskPolicy(
        controller=controller,
        bridge=bridge,
        prompt="fix it",
        diagnosis_model=DIAGNOSIS_MODEL,
        execution_model=EXECUTION_MODEL,
        repo="C:/repo",
    )

    diagnosis = await policy.select(state)
    assert diagnosis.kind is ControllerDecisionKind.INVOKE_CAPABILITY
    assert diagnosis.capability == "reason.generate"
    assert diagnosis.parameters["model"] == DIAGNOSIS_MODEL
    _record_cognitive_result(controller, state.id, diagnosis)

    closure_decision = await policy.select(state)
    assert closure_decision.kind is ControllerDecisionKind.FORM_CLOSURE
    assert closure_decision.closure is not None
    assert closure_decision.closure.problem_ref == assessment_ref
    assert closure_decision.closure.requested_capabilities == ["shell.exec"]
    assert runtime.list_work() == []

    closure_state = await controller.apply(closure_decision)
    proposal_decision = await policy.select(closure_state)
    assert proposal_decision.kind is ControllerDecisionKind.PROPOSE_WORK
    assert proposal_decision.closure_ref == closure_state.active_closure_ref
    assert runtime.list_work() == []

    waiting = await controller.apply(proposal_decision)
    assert waiting.status is ControllerStatus.WAITING
    assert waiting.work_proposal_ref is not None
    assert runtime.list_work() == []

    work = bridge.materialize_work(waiting)
    assert work.metadata["responsibility_proposal_ref"] == waiting.work_proposal_ref
    assert work.metadata["external_effect_authority"] == "required-separately"
    assert work.requested_capabilities == ["shell.exec"]


async def test_manual_task_returns_materialized_work_reality_through_revision() -> None:
    runtime, controller, bridge, state, _assessment_ref = _setup()
    policy = ManualTaskPolicy(
        controller=controller,
        bridge=bridge,
        prompt="fix it",
        diagnosis_model=DIAGNOSIS_MODEL,
        execution_model=EXECUTION_MODEL,
        repo="C:/repo",
    )
    diagnosis = await policy.select(state)
    _record_cognitive_result(controller, state.id, diagnosis)
    closure_state = await controller.apply(await policy.select(state))
    waiting = await controller.apply(await policy.select(closure_state))
    work = bridge.materialize_work(waiting)

    bridge.record_capability_result(
        controller_id=state.id,
        work_id=work.id,
        run_id="run_test",
        stage="execution",
        capability="shell.exec",
        result=_capability_result(status="succeeded", message="tests passed"),
    )
    revision_decision = await policy.select(waiting)
    assert revision_decision.kind is ControllerDecisionKind.ASSESS_REVISION
    assert revision_decision.revision is not None
    assert revision_decision.revision.work_ref == work.id
    assert revision_decision.revision.closure_ref == waiting.active_closure_ref
    assert revision_decision.revision.recommended_disposition.value == "close"

    closed = await controller.apply(revision_decision)
    assert closed.status is ControllerStatus.CLOSED
    assert closed.last_revision_ref == revision_decision.revision.id
    assert runtime.get_work(work.id) is not None


async def test_failed_manual_work_needs_revision_then_explicit_human_reopen() -> None:
    _runtime, controller, bridge, state, _assessment_ref = _setup()
    policy = ManualTaskPolicy(
        controller=controller,
        bridge=bridge,
        prompt="fix it",
        diagnosis_model=DIAGNOSIS_MODEL,
        execution_model=EXECUTION_MODEL,
        repo="C:/repo",
    )
    diagnosis = await policy.select(state)
    _record_cognitive_result(controller, state.id, diagnosis)
    closure_state = await controller.apply(await policy.select(state))
    waiting = await controller.apply(await policy.select(closure_state))
    work = bridge.materialize_work(waiting)
    bridge.record_capability_result(
        controller_id=state.id,
        work_id=work.id,
        run_id="run_failed",
        stage="execution",
        capability="shell.exec",
        result=_capability_result(status="failed", message="test failed"),
    )

    failure_revision = await policy.select(waiting)
    assert failure_revision.kind is ControllerDecisionKind.ASSESS_REVISION
    failed_waiting = await controller.apply(failure_revision)
    assert failed_waiting.status is ControllerStatus.WAITING

    controller.store.append_event(
        Event(
            id=new_id("event"),
            type="PersonalHumanInstructionObserved",
            subject_ref=state.id,
            payload={"command": "try the bounded alternative", "actor_ref": "principal:test"},
        )
    )
    resumed_policy = ManualTaskPolicy(
        controller=controller,
        bridge=bridge,
        prompt="fix it",
        diagnosis_model=DIAGNOSIS_MODEL,
        execution_model=EXECUTION_MODEL,
        repo="C:/repo",
        human_instruction="try the bounded alternative",
    )
    human_revision = await resumed_policy.select(failed_waiting)
    assert human_revision.kind is ControllerDecisionKind.ASSESS_REVISION
    assert human_revision.revision is not None
    assert human_revision.revision.recommended_disposition.value == "reopen-cognition"

    reopen_required = await controller.apply(human_revision)
    assert reopen_required.status is ControllerStatus.REOPEN_REQUIRED
    reopen = await resumed_policy.select(reopen_required)
    assert reopen.kind is ControllerDecisionKind.REOPEN


async def test_alert_policy_closure_declares_execution_apply_and_verification() -> None:
    runtime, controller, bridge, state, assessment_ref = _setup(kind="personal-incident-repair")
    policy = AutonomousRepairPolicy(
        controller=controller,
        bridge=bridge,
        prompt="alert fact",
        diagnosis_model=DIAGNOSIS_MODEL,
        execution_model=EXECUTION_MODEL,
        repo="C:/repo",
        project="test",
        verification_labels={"alertname": "Broken"},
    )
    diagnosis = await policy.select(state)
    assert diagnosis.kind is ControllerDecisionKind.INVOKE_CAPABILITY
    assert diagnosis.capability == "reason.generate"
    _record_cognitive_result(controller, state.id, diagnosis, message="repair plan")

    closure = await policy.select(state)
    assert closure.kind is ControllerDecisionKind.FORM_CLOSURE
    assert closure.closure is not None
    assert closure.closure.problem_ref == assessment_ref
    assert closure.closure.requested_capabilities == [
        "shell.exec",
        "docker.compose.up",
        "monitor.alert.active",
    ]
    assert closure.closure.effect_class.value == "external-effect"
    assert runtime.list_work() == []


def _capability_result(*, status: str, message: str):
    from portable_runtime.core.capabilities import CapabilityResult

    return CapabilityResult(
        request_id=new_id("request"),
        provider_id="test",
        status=status,
        message=message,
    )
