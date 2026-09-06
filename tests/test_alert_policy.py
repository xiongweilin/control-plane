import pytest
from portable_runtime.controller import (
    CognitiveController,
    ControllerDecision,
    ControllerDecisionKind,
    ControllerStatus,
    RevisionDisposition,
)
from portable_runtime.core.models import Event, new_id
from portable_runtime.core.runtime import Runtime
from portable_runtime.responsibility import EffectClass

from control_plane.alert_policy import AutonomousRepairPolicy, ManualTaskPolicy, classify_safety
from control_plane.kernel_bridge import PersonalKernelBridge

DIAGNOSIS_MODEL = "codex/gpt-5.6-luna"
EXECUTION_MODEL = "codex/gpt-5.6-luna"
SYNC_OLD_SHA = "0" * 40
SYNC_REMOTE_SHA = "1" * 40
SYNC_NEW_SHA = "2" * 40
SYNC_SOURCE_SHA = "3" * 40


def _setup(
    *, kind: str = "personal-command", verification_labels: dict[str, str] | None = None
):
    runtime = Runtime(runtime_id="personal-platform")
    controller = CognitiveController(runtime)
    bridge = PersonalKernelBridge(runtime, controller, owner_principal="principal:test")
    labels = (
        verification_labels
        if verification_labels is not None
        else {"alertname": "Broken"} if kind == "personal-incident-repair" else {}
    )
    state, assessment_ref = bridge.begin(
        title="Personal task",
        description="fix it",
        kind=kind,
        repo="C:/repo",
        project="test" if kind == "personal-incident-repair" else None,
        verification_labels=labels,
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


async def _materialize_repair_work(policy, controller, state):
    closure_state = await controller.apply(await policy.select(state))
    waiting = await controller.apply(await policy.select(closure_state))
    return waiting, policy.bridge.materialize_work(waiting)


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
    assert diagnosis.parameters["attempt_index"] == 1
    assert diagnosis.parameters["timeout_seconds"] == 900.0
    assert "attempt 1/2" in diagnosis.instruction
    _record_cognitive_result(
        controller,
        state.id,
        diagnosis,
        message="SAFETY_CLASS=REVERSIBLE\nrepair plan",
    )

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


@pytest.mark.asyncio
async def test_autonomous_repair_execution_uses_the_nine_hundred_second_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, controller, bridge, state, _assessment_ref = _setup(
        kind="personal-incident-repair"
    )
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

    assert policy.attempt_limit == 2
    assert not hasattr(policy, "max_attempts")
    diagnosis = await policy.select(state)
    _record_cognitive_result(
        controller,
        state.id,
        diagnosis,
        message="SAFETY_CLASS=REVERSIBLE\nrepair plan",
    )
    waiting, _work = await _materialize_repair_work(policy, controller, state)

    calls = []

    async def fake_run_capability(work_id, capability, **kwargs):
        calls.append((work_id, capability, kwargs))
        return _capability_result(status="succeeded", message="bounded result")

    monkeypatch.setattr(runtime, "run_capability", fake_run_capability)
    await policy.execute_work(waiting)

    execution_calls = [
        item for item in calls if item[2].get("phase") == "execution"
    ]
    assert len(execution_calls) == 1
    assert execution_calls[0][2]["timeout_seconds"] == 900.0
    assert execution_calls[0][2]["attempt_index"] == 1


@pytest.mark.asyncio
async def test_unknown_safety_uses_only_read_only_execution_and_verification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, controller, bridge, state, _assessment_ref = _setup(
        kind="personal-incident-repair"
    )
    monkeypatch.setattr(AutonomousRepairPolicy, "_repo_is_dirty", lambda _policy: False)
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
    _record_cognitive_result(
        controller,
        state.id,
        diagnosis,
        message="SAFETY_CLASS=UNKNOWN\nmissing evidence",
    )
    waiting, _work = await _materialize_repair_work(policy, controller, state)

    calls = []

    async def fake_run_capability(work_id, capability, **kwargs):
        calls.append((work_id, capability, kwargs))
        if capability == "monitor.alert.active":
            return _capability_result(
                status="succeeded", message="alert remains active"
            ).model_copy(update={"metadata": {"active": True}})
        return _capability_result(status="succeeded", message="read-only evidence")

    monkeypatch.setattr(runtime, "run_capability", fake_run_capability)
    await policy.execute_work(waiting)

    assert [
        (capability, kwargs["phase"])
        for _work_id, capability, kwargs in calls
    ] == [
        ("reason.generate", "execution"),
        ("monitor.alert.active", "verification"),
    ]
    execution_call = calls[0][2]
    assert execution_call["timeout_seconds"] == 900.0
    assert execution_call["attempt_index"] == 1
    assert "UNKNOWN" in execution_call["instruction"]
    assert all(
        event.payload["capability"]
        not in {"shell.exec", "git.fast_forward", "git.push_exact_ref", "chezmoi.apply"}
        for event in bridge.result_events(state.id, work_id=_work.id)
    )


@pytest.mark.asyncio
async def test_unresolved_alert_runs_exactly_two_diagnosis_and_execution_rounds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, controller, bridge, state, _assessment_ref = _setup(
        kind="personal-incident-repair"
    )
    policy = AutonomousRepairPolicy(
        controller=controller,
        bridge=bridge,
        prompt="alert fact",
        diagnosis_model=DIAGNOSIS_MODEL,
        execution_model=EXECUTION_MODEL,
        repo="C:/repo",
        project=None,
        verification_labels={"alertname": "Broken"},
    )
    calls = []

    async def fake_run_capability(work_id, capability, **kwargs):
        calls.append((work_id, capability, kwargs))
        if capability == "monitor.alert.active":
            return _capability_result(
                status="succeeded",
                message="alert remains active",
            ).model_copy(update={"metadata": {"active": True}})
        return _capability_result(status="succeeded", message="bounded result")

    monkeypatch.setattr(runtime, "run_capability", fake_run_capability)

    diagnosis = await policy.select(state)
    _record_cognitive_result(
        controller,
        state.id,
        diagnosis,
        message="SAFETY_CLASS=REVERSIBLE\nrepair plan",
    )
    closure_state = await controller.apply(await policy.select(state))
    waiting = await controller.apply(await policy.select(closure_state))
    work = bridge.materialize_work(waiting)
    await policy.execute_work(waiting)

    first_revision = await policy.select(waiting)
    assert first_revision.revision is not None
    assert first_revision.revision.recommended_disposition is RevisionDisposition.REOPEN_COGNITION
    reopened_required = await controller.apply(first_revision)
    reopened = await controller.apply(await policy.select(reopened_required))
    second_diagnosis = await policy.select(reopened)
    assert second_diagnosis.parameters["attempt_index"] == 2
    assert second_diagnosis.parameters["timeout_seconds"] == 900.0
    _record_cognitive_result(
        controller,
        reopened.id,
        second_diagnosis,
        message="SAFETY_CLASS=REVERSIBLE\nsecond bounded plan",
    )
    second_closure = await controller.apply(await policy.select(reopened))
    second_waiting = await controller.apply(await policy.select(second_closure))
    second_work = bridge.materialize_work(second_waiting)
    await policy.execute_work(second_waiting)

    final_revision = await policy.select(second_waiting)
    assert final_revision.revision is not None
    assert final_revision.revision.recommended_disposition is RevisionDisposition.WAIT
    assert policy._diagnosis_count(controller.get(state.id)) == 2
    assert policy._execution_count(controller.get(state.id)) == 2
    assert len(bridge.result_events(state.id, work_id=work.id)) == 2
    assert len(bridge.result_events(state.id, work_id=second_work.id)) == 2
    execution_calls = [
        item for item in calls if item[2].get("phase") == "execution"
    ]
    assert len(execution_calls) == 2
    assert [item[2]["attempt_index"] for item in execution_calls] == [1, 2]
    assert [item[2]["timeout_seconds"] for item in execution_calls] == [900.0, 900.0]


@pytest.mark.parametrize(
    ("message", "blocker", "repo_is_dirty"),
    [
        (
            "SAFETY_CLASS=IRREVERSIBLE\nowner decision required",
            "irreversible",
            False,
        ),
        (
            "SAFETY_CLASS=REVERSIBLE\nEXECUTION_BLOCKER=DIRTY_REPO\nowner review required",
            "dirty-repository",
            False,
        ),
        (
            "SAFETY_CLASS=REVERSIBLE\nowner review required",
            "dirty-repository",
            True,
        ),
    ],
)
@pytest.mark.asyncio
async def test_first_diagnosis_blocker_stops_effect_execution(
    message: str, blocker: str, repo_is_dirty: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    _runtime, controller, bridge, state, _assessment_ref = _setup(
        kind="personal-incident-repair"
    )
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
    monkeypatch.setattr(
        AutonomousRepairPolicy, "_repo_is_dirty", lambda _policy: repo_is_dirty
    )

    diagnosis = await policy.select(state)
    assert diagnosis.parameters["attempt_index"] == 1
    _record_cognitive_result(controller, state.id, diagnosis, message=message)
    assert policy.diagnosis_blocker(state) == blocker

    waiting, work = await _materialize_repair_work(policy, controller, state)
    await policy.execute_work(waiting)

    results = bridge.result_events(state.id, work_id=work.id)
    assert [(event.payload["stage"], event.payload["capability"]) for event in results] == [
        ("execution", "execution.blocked")
    ]
    assert results[0].payload["result"]["metadata"]["blocker"] == blocker

    revision_decision = await policy.select(waiting)
    assert revision_decision.revision is not None
    assert revision_decision.revision.failure_class == "execution-blocked"
    assert revision_decision.revision.recommended_disposition is RevisionDisposition.WAIT


@pytest.mark.parametrize(
    ("capability", "fields", "effect_class"),
    [
        (
            "git.fast_forward",
            (
                "SYNC_REMOTE=origin",
                "SYNC_BRANCH=main",
                f"SYNC_EXPECTED_OLD_SHA={SYNC_OLD_SHA}",
                f"SYNC_EXPECTED_REMOTE_SHA={SYNC_REMOTE_SHA}",
            ),
            EffectClass.INTERNAL_REVERSIBLE,
        ),
        (
            "git.push_exact_ref",
            (
                "SYNC_REMOTE=origin",
                "SYNC_BRANCH=main",
                f"SYNC_EXPECTED_OLD_SHA={SYNC_OLD_SHA}",
                f"SYNC_EXPECTED_NEW_SHA={SYNC_NEW_SHA}",
            ),
            EffectClass.EXTERNAL_EFFECT,
        ),
        (
            "chezmoi.apply",
            (
                "SYNC_SOURCE_DIR=C:/Users/metra/.local/share/chezmoi",
                f"SYNC_EXPECTED_SOURCE_SHA={SYNC_SOURCE_SHA}",
            ),
            EffectClass.INTERNAL_REVERSIBLE,
        ),
    ],
)
@pytest.mark.asyncio
async def test_sync_alert_selects_only_the_exact_kernel_capability(
    capability: str, fields: tuple[str, ...], effect_class: EffectClass
) -> None:
    _runtime, controller, bridge, state, _assessment_ref = _setup(
        kind="personal-incident-repair",
        verification_labels={"alertname": "ControlPlaneSynchronizationDegraded"},
    )
    policy = AutonomousRepairPolicy(
        controller=controller,
        bridge=bridge,
        prompt="synchronization drift",
        diagnosis_model=DIAGNOSIS_MODEL,
        execution_model=EXECUTION_MODEL,
        repo="C:/repo",
        project="test",
        verification_labels={"alertname": "ControlPlaneSynchronizationDegraded"},
    )

    diagnosis = await policy.select(state)
    _record_cognitive_result(
        controller,
        state.id,
        diagnosis,
        message="\n".join(("SAFETY_CLASS=REVERSIBLE", f"SYNC_CAPABILITY={capability}", *fields)),
    )
    closure = await policy.select(state)

    assert closure.closure is not None
    assert closure.closure.requested_capabilities == [
        "reason.generate",
        capability,
        "monitor.alert.active",
    ]
    assert closure.closure.effect_class is effect_class


@pytest.mark.parametrize(
    ("capability", "fields"),
    [
        (
            "git.fast_forward",
            (
                "SYNC_REMOTE=origin",
                "SYNC_BRANCH=main",
                f"SYNC_EXPECTED_OLD_SHA={SYNC_OLD_SHA}",
                f"SYNC_EXPECTED_REMOTE_SHA={SYNC_REMOTE_SHA}",
            ),
        ),
        (
            "git.push_exact_ref",
            (
                "SYNC_REMOTE=origin",
                "SYNC_BRANCH=main",
                f"SYNC_EXPECTED_OLD_SHA={SYNC_OLD_SHA}",
                f"SYNC_EXPECTED_NEW_SHA={SYNC_NEW_SHA}",
            ),
        ),
        (
            "chezmoi.apply",
            (
                "SYNC_SOURCE_DIR=C:/Users/metra/.local/share/chezmoi",
                f"SYNC_EXPECTED_SOURCE_SHA={SYNC_SOURCE_SHA}",
            ),
        ),
    ],
)
@pytest.mark.asyncio
async def test_sync_execution_delegates_only_to_the_selected_exact_capability(
    capability: str, fields: tuple[str, ...], monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, controller, bridge, state, _assessment_ref = _setup(
        kind="personal-incident-repair",
        verification_labels={"alertname": "ControlPlaneSynchronizationDegraded"},
    )
    policy = AutonomousRepairPolicy(
        controller=controller,
        bridge=bridge,
        prompt="synchronization drift",
        diagnosis_model=DIAGNOSIS_MODEL,
        execution_model=EXECUTION_MODEL,
        repo="C:/repo",
        project="test",
        verification_labels={"alertname": "ControlPlaneSynchronizationDegraded"},
    )

    diagnosis = await policy.select(state)
    _record_cognitive_result(
        controller,
        state.id,
        diagnosis,
        message="\n".join(("SAFETY_CLASS=REVERSIBLE", f"SYNC_CAPABILITY={capability}", *fields)),
    )
    waiting, work = await _materialize_repair_work(policy, controller, state)
    calls = []

    async def fake_run_capability(work_id, called_capability, **kwargs):
        calls.append((work_id, called_capability, kwargs))
        if called_capability == "monitor.alert.active":
            return _capability_result(
                status="succeeded", message="alert remains active"
            ).model_copy(update={"metadata": {"active": True}})
        return _capability_result(status="succeeded", message="bounded result")

    monkeypatch.setattr(runtime, "run_capability", fake_run_capability)
    await policy.execute_work(waiting)

    assert [
        (called_capability, kwargs["phase"])
        for _work_id, called_capability, kwargs in calls
    ] == [
        ("reason.generate", "execution"),
        (capability, "apply"),
        ("monitor.alert.active", "verification"),
    ]
    apply_call = calls[1][2]
    assert apply_call["repo"] == "C:/repo"
    assert len(apply_call["subject_version_refs"]) == 1
    assert not any(
        called_capability in {"shell.exec", "git.merge", "git.push", "git.rollback"}
        for _work_id, called_capability, _kwargs in calls
    )
    assert len(bridge.result_events(state.id, work_id=work.id)) == 3


@pytest.mark.asyncio
async def test_incomplete_sync_selection_falls_back_to_read_only_closure() -> None:
    _runtime, controller, bridge, state, _assessment_ref = _setup(
        kind="personal-incident-repair",
        verification_labels={"alertname": "ControlPlaneSynchronizationDegraded"},
    )
    policy = AutonomousRepairPolicy(
        controller=controller,
        bridge=bridge,
        prompt="synchronization drift",
        diagnosis_model=DIAGNOSIS_MODEL,
        execution_model=EXECUTION_MODEL,
        repo="C:/repo",
        project="test",
        verification_labels={"alertname": "ControlPlaneSynchronizationDegraded"},
    )

    diagnosis = await policy.select(state)
    _record_cognitive_result(
        controller,
        state.id,
        diagnosis,
        message=(
            "SAFETY_CLASS=REVERSIBLE\n"
            "SYNC_CAPABILITY=git.fast_forward\n"
            "SYNC_REMOTE=origin\n"
            "SYNC_BRANCH=main\n"
            f"SYNC_EXPECTED_OLD_SHA={SYNC_OLD_SHA}"
        ),
    )
    closure = await policy.select(state)

    assert closure.closure is not None
    assert closure.closure.requested_capabilities == [
        "reason.generate",
        "monitor.alert.active",
    ]
    assert closure.closure.effect_class is EffectClass.READ_ONLY


async def test_alert_policy_can_declare_bounded_automatic_maintenance() -> None:
    _runtime, controller, bridge, state, _assessment_ref = _setup(kind="personal-incident-repair")
    policy = AutonomousRepairPolicy(
        controller=controller,
        bridge=bridge,
        prompt="known garbage path is present",
        diagnosis_model=DIAGNOSIS_MODEL,
        execution_model=EXECUTION_MODEL,
        verification_labels={"alertname": "ControlPlaneGarbageDetected"},
        maintenance_capability="maintenance.cleanup_known_garbage",
    )

    diagnosis = await policy.select(state)
    _record_cognitive_result(
        controller,
        state.id,
        diagnosis,
        message="SAFETY_CLASS=REVERSIBLE\nquarantine exact path",
    )
    closure = await policy.select(state)

    assert closure.closure is not None
    assert closure.closure.requested_capabilities == [
        "reason.generate",
        "maintenance.cleanup_known_garbage",
        "monitor.alert.active",
    ]


async def test_line_ending_cleanup_is_a_provider_only_reversible_effect() -> None:
    _runtime, controller, bridge, state, _assessment_ref = _setup(kind="personal-incident-repair")
    policy = AutonomousRepairPolicy(
        controller=controller,
        bridge=bridge,
        prompt="ratio contains line-ending-only worktree noise",
        diagnosis_model=DIAGNOSIS_MODEL,
        execution_model=EXECUTION_MODEL,
        repo="D:/agent/ratio",
        project="ratio",
        verification_labels={"alertname": "ControlPlaneSynchronizationDegraded"},
        maintenance_capability="git.discard_line_ending_changes",
        maintenance_parameters={"repo": "D:/agent/ratio", "project": "ratio"},
    )

    diagnosis = await policy.select(state)
    _record_cognitive_result(
        controller,
        state.id,
        diagnosis,
        message="SAFETY_CLASS=REVERSIBLE\nline endings only",
    )
    closure = await policy.select(state)

    assert closure.closure is not None
    assert closure.closure.requested_capabilities == [
        "reason.generate",
        "git.discard_line_ending_changes",
        "monitor.alert.active",
    ]
    assert closure.closure.effect_class is EffectClass.INTERNAL_REVERSIBLE


def test_safety_class_requires_the_current_codex_marker() -> None:
    assert (
        classify_safety({"status": "succeeded", "message": "SAFETY_CLASS=REVERSIBLE\nplan"})
        == "reversible"
    )
    assert (
        classify_safety({"status": "succeeded", "message": "SAFETY_CLASS=IRREVERSIBLE\nplan"})
        == "irreversible"
    )
    assert classify_safety({"status": "succeeded", "message": "historical label only"}) == "unknown"


async def test_non_reversible_judgment_forms_read_only_closure() -> None:
    _runtime, controller, bridge, state, _assessment_ref = _setup(kind="personal-incident-repair")
    policy = AutonomousRepairPolicy(
        controller=controller,
        bridge=bridge,
        prompt="unsafe alert",
        diagnosis_model=DIAGNOSIS_MODEL,
        execution_model=EXECUTION_MODEL,
        repo="C:/repo",
        project="test",
        verification_labels={"alertname": "Unsafe"},
    )
    diagnosis = await policy.select(state)
    _record_cognitive_result(
        controller,
        state.id,
        diagnosis,
        message="SAFETY_CLASS=IRREVERSIBLE\nrequires owner decision",
    )
    closure = await policy.select(state)
    assert closure.closure is not None
    assert closure.closure.requested_capabilities == [
        "reason.generate",
        "monitor.alert.active",
    ]
    assert closure.closure.effect_class is EffectClass.READ_ONLY


def _capability_result(*, status: str, message: str):
    from portable_runtime.core.capabilities import CapabilityResult

    return CapabilityResult(
        request_id=new_id("request"),
        provider_id="test",
        status=status,
        message=message,
    )
