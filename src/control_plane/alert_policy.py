from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from portable_runtime.controller import (
    CognitiveClosure,
    CognitiveController,
    ControllerDecision,
    ControllerDecisionKind,
    ControllerState,
    ControllerStatus,
    RevisionAssessment,
    RevisionDisposition,
    RevisionScope,
)
from portable_runtime.core.models import Event
from portable_runtime.responsibility import EffectClass

from .kernel_bridge import PersonalKernelBridge

_RESULT_EVENT = "ControllerCapabilityResultObserved"
_DECISION_EVENT = "ControllerDecisionSelected"


def _ordered_decisions(
    controller: CognitiveController,
    controller_id: str,
) -> list[tuple[Event, ControllerDecision]]:
    values: list[tuple[Event, ControllerDecision]] = []
    for event in controller.store.list_events(controller_id):
        if event.type != _DECISION_EVENT:
            continue
        raw = event.payload.get("decision")
        if isinstance(raw, dict):
            values.append((event, ControllerDecision.model_validate(raw)))
    return sorted(values, key=lambda item: item[0].created_at)


def _result_by_decision(
    controller: CognitiveController,
    controller_id: str,
) -> dict[str, dict[str, Any]]:
    values: dict[str, dict[str, Any]] = {}
    for event in controller.store.list_events(controller_id):
        if event.type != _RESULT_EVENT:
            continue
        decision_ref = event.payload.get("decision_ref")
        raw = event.payload.get("result")
        if isinstance(decision_ref, str) and isinstance(raw, dict):
            values[decision_ref] = dict(raw)
    return values


def _message(result: dict[str, Any] | None) -> str:
    if not result:
        return ""
    return str(result.get("message", ""))[-12_000:]


def _succeeded(result: dict[str, Any] | None) -> bool:
    return result is not None and str(result.get("status", "")) == "succeeded"


def _event_result(event: Event) -> dict[str, Any]:
    raw = event.payload.get("result")
    return dict(raw) if isinstance(raw, dict) else {}


def _run_ref(events: list[Event]) -> str | None:
    for event in reversed(events):
        value = event.payload.get("run_ref")
        if isinstance(value, str) and value:
            return value
    return None


def _latest_run(runtime: Any, work_id: str) -> Any | None:
    runs = runtime.store.list_runs(work_id)
    if not runs:
        return None
    return max(runs, key=lambda run: (run.started_at or run.created_at, run.created_at))


@dataclass(frozen=True, slots=True)
class AutonomousRepairPolicy:
    """Personal incident policy executed through the Agent Kernel v2 loop.

    Diagnosis is read-class cognition. A successful diagnosis must form a
    CognitiveClosure and hand it to WorkProposal. The profile then admits and
    materializes Work through ResponsibilityKernel, executes effects through the
    Runtime boundary, and returns reality through RevisionAssessment. No direct
    controller-to-effect or direct reopen-to-Work path remains.
    """

    controller: CognitiveController = field(repr=False, compare=False)
    bridge: PersonalKernelBridge = field(repr=False, compare=False)
    prompt: str
    diagnosis_model: str
    execution_model: str
    repo: str | None = None
    project: str | None = None
    verification_labels: dict[str, str] = field(default_factory=dict)
    human_instruction: str | None = None
    max_attempts: int = 2
    maintenance_capability: str | None = None
    maintenance_parameters: dict[str, Any] = field(default_factory=dict)
    fail_safe: bool = False

    @property
    def policy_ref(self) -> str:
        return "control-plane/autonomous-repair-v2"

    def _session_cutoff(self, controller_id: str) -> datetime | None:
        if not self.human_instruction:
            return None
        event = self.bridge.latest_human_instruction_event(controller_id)
        return event.created_at if event is not None else None

    def _diagnosis_count(self, state: ControllerState) -> int:
        cutoff = self._session_cutoff(state.id)
        count = 0
        for event, decision in _ordered_decisions(self.controller, state.id):
            if cutoff is not None and event.created_at <= cutoff:
                continue
            if (
                decision.kind is ControllerDecisionKind.INVOKE_CAPABILITY
                and decision.capability == "reason.generate"
                and decision.parameters.get("phase") == "diagnosis"
            ):
                count += 1
        return count

    def _diagnosis(self, state: ControllerState, *, retry_context: str = "") -> ControllerDecision:
        attempt = self._diagnosis_count(state) + 1
        instruction = (
            f"Diagnose this incident for autonomous repair attempt {attempt}/"
            f"{self.max_attempts}.\n"
            "Identify the most likely root cause and give one concrete bounded execution "
            "instruction. Do not claim recovery or completion. Prefer the smallest repair "
            "that can be tested.\n\n"
            f"Incident:\n{self.prompt}"
        )
        if self.human_instruction:
            instruction += (
                "\n\nHuman instruction (authoritative task direction, not truth):\n"
                f"{self.human_instruction}"
            )
        if retry_context:
            instruction += f"\n\nPrevious attempt evidence:\n{retry_context}"
        parameters: dict[str, Any] = {"model": self.diagnosis_model, "phase": "diagnosis"}
        if self.repo:
            parameters["repo"] = self.repo
        return ControllerDecision(
            controller_ref=state.id,
            state_version=state.version,
            kind=ControllerDecisionKind.INVOKE_CAPABILITY,
            capability="reason.generate",
            instruction=instruction,
            parameters=parameters,
            reason="collect a bounded diagnosis before cognitive closure",
        )

    def _requested_capabilities(self) -> list[str]:
        values = ["shell.exec" if self.repo else "reason.generate"]
        if self.project:
            values.append("docker.compose.up")
        if self.maintenance_capability:
            values.append(self.maintenance_capability)
        values.append("monitor.alert.active")
        return values

    def _effect_class(self) -> EffectClass:
        if self.project:
            return EffectClass.EXTERNAL_EFFECT
        if self.repo:
            return EffectClass.INTERNAL_REVERSIBLE
        if self.maintenance_capability:
            return EffectClass.INTERNAL_REVERSIBLE
        return EffectClass.READ_ONLY

    def _form_closure(
        self,
        state: ControllerState,
        diagnosis_result: dict[str, Any] | None,
    ) -> ControllerDecision:
        assessment_ref = self.bridge.assessment_ref(state)
        basis_refs = [assessment_ref]
        if state.last_result_ref:
            basis_refs.append(state.last_result_ref)
        closure = CognitiveClosure(
            controller_ref=state.id,
            controller_state_version=state.version,
            responsibility_ref=state.responsibility_ref,
            subject_ref=state.subject_ref,
            problem_ref=assessment_ref,
            basis_refs=basis_refs,
            selected_direction=(
                "execute the smallest bounded repair selected by the current diagnosis"
            ),
            rationale=_message(diagnosis_result),
            acceptance_criteria=["the triggering alert is no longer active"],
            verification_plan=["query monitor.alert.active after the materialized Work executes"],
            stop_conditions=["stop after two unresolved autonomous attempts"],
            escalation_conditions=["wait for an explicit owner command after the attempt budget"],
            reopen_conditions=[
                "execution fails",
                "project apply fails",
                "monitoring shows the triggering alert remains active",
            ],
            requested_capabilities=self._requested_capabilities(),
            effect_class=self._effect_class(),
        )
        return ControllerDecision(
            controller_ref=state.id,
            state_version=state.version,
            kind=ControllerDecisionKind.FORM_CLOSURE,
            closure=closure,
            reason="turn the current diagnosis into an explicit bounded cognitive closure",
        )

    def _propose_work(self, state: ControllerState) -> ControllerDecision:
        if not state.active_closure_ref:
            raise ValueError("repair proposal requires an active cognitive closure")
        context = self.bridge.context(state)
        return ControllerDecision(
            controller_ref=state.id,
            state_version=state.version,
            kind=ControllerDecisionKind.PROPOSE_WORK,
            closure_ref=state.active_closure_ref,
            assessment_ref=self.bridge.assessment_ref(state),
            work_kind="personal-incident-repair",
            work_title=context.title,
            work_description=context.description,
            requested_capabilities=self._requested_capabilities(),
            expected_result="the triggering alert is no longer active",
            stop_conditions=["stop after the bounded autonomous attempt budget"],
            escalation_conditions=["require an explicit owner command before further attempts"],
            effect_class=self._effect_class().value,
            reason="handoff the closure to the persistent responsibility kernel as WorkProposal",
        )

    def _current_revision(self, state: ControllerState) -> RevisionAssessment | None:
        if not state.last_revision_ref or not state.active_closure_ref:
            return None
        work = self.bridge.work_for_state(state)
        if work is None:
            return None
        for revision in self.controller.revisions(state.id):
            if (
                revision.id == state.last_revision_ref
                and revision.closure_ref == state.active_closure_ref
                and revision.work_ref == work.id
            ):
                return revision
        return None

    def _revision(self, state: ControllerState) -> ControllerDecision:
        work = self.bridge.work_for_state(state)
        if work is None or not state.active_closure_ref:
            raise ValueError("repair revision requires materialized Work and active closure")
        events = self.bridge.result_events(state.id, work_id=work.id)
        if not events:
            raise ValueError("repair revision requires durable Work result observations")

        execution = next(
            (event for event in events if event.payload.get("stage") == "execution"),
            None,
        )
        apply_event = next(
            (event for event in events if event.payload.get("stage") == "apply"),
            None,
        )
        verification = next(
            (event for event in events if event.payload.get("stage") == "verification"),
            None,
        )
        attempts = self._diagnosis_count(state)
        failed_event = next(
            (
                event
                for event in (execution, apply_event)
                if event is not None and not _succeeded(_event_result(event))
            ),
            None,
        )

        outcome_refs = [event.id for event in (execution, apply_event) if event is not None]
        verification_refs = [verification.id] if verification is not None else []
        reason_refs = [event.id for event in events]

        if failed_event is not None:
            disposition = (
                RevisionDisposition.REOPEN_COGNITION
                if attempts < self.max_attempts
                else RevisionDisposition.WAIT
            )
            scope = RevisionScope.EXECUTION
            reason = "the materialized repair Work failed before recovery could be verified"
            failure_class = "execution-failure"
        elif verification is None:
            disposition = (
                RevisionDisposition.REOPEN_COGNITION
                if attempts < self.max_attempts
                else RevisionDisposition.WAIT
            )
            scope = RevisionScope.VERIFICATION
            reason = "the repair produced no durable monitoring verification"
            failure_class = "verification-missing"
        else:
            verify_result = _event_result(verification)
            metadata = verify_result.get("metadata", {})
            active = metadata.get("active") if isinstance(metadata, dict) else None
            if _succeeded(verify_result) and active is False:
                disposition = RevisionDisposition.CLOSE
                scope = RevisionScope.VERIFICATION
                reason = "monitoring confirms the triggering alert is no longer active"
                failure_class = ""
            else:
                disposition = (
                    RevisionDisposition.REOPEN_COGNITION
                    if attempts < self.max_attempts
                    else RevisionDisposition.WAIT
                )
                scope = RevisionScope.PROBLEM_DEFINITION
                reason = "reality still contradicts the current repair closure"
                failure_class = "alert-still-active"

        revision = RevisionAssessment(
            controller_ref=state.id,
            controller_state_version=state.version,
            work_ref=work.id,
            closure_ref=state.active_closure_ref,
            run_ref=_run_ref(events),
            outcome_refs=outcome_refs,
            verification_refs=verification_refs,
            reason_refs=reason_refs,
            failure_class=failure_class,
            revision_scope=scope,
            recommended_disposition=disposition,
            reason=reason,
            carry_forward_refs=[self.bridge.assessment_ref(state)],
        )
        return ControllerDecision(
            controller_ref=state.id,
            state_version=state.version,
            kind=ControllerDecisionKind.ASSESS_REVISION,
            revision=revision,
            reason="return materialized Work reality to the cognitive controller",
        )

    def _human_reopen_revision(self, state: ControllerState) -> ControllerDecision:
        work = self.bridge.work_for_state(state)
        if work is None or not state.active_closure_ref:
            raise ValueError("human continuation requires materialized Work and active closure")
        events = self.bridge.result_events(state.id, work_id=work.id)
        outcome_refs = [
            event.id for event in events if event.payload.get("stage") != "verification"
        ]
        verification_refs = [
            event.id for event in events if event.payload.get("stage") == "verification"
        ]
        if not outcome_refs and not verification_refs:
            raise ValueError("human continuation has no prior reality observation to revise")
        instruction_event = self.bridge.latest_human_instruction_event(state.id)
        reason_refs = [event.id for event in events]
        if instruction_event is not None:
            reason_refs.append(instruction_event.id)
        revision = RevisionAssessment(
            controller_ref=state.id,
            controller_state_version=state.version,
            work_ref=work.id,
            closure_ref=state.active_closure_ref,
            run_ref=_run_ref(events),
            outcome_refs=outcome_refs,
            verification_refs=verification_refs,
            reason_refs=reason_refs,
            revision_scope=RevisionScope.DECISION,
            recommended_disposition=RevisionDisposition.REOPEN_COGNITION,
            reason="an explicit owner command requests a new cognitive pass over prior reality",
            carry_forward_refs=[self.bridge.assessment_ref(state)],
        )
        return ControllerDecision(
            controller_ref=state.id,
            state_version=state.version,
            kind=ControllerDecisionKind.ASSESS_REVISION,
            revision=revision,
            reason="explicit owner direction reopens cognition through RevisionAssessment",
        )

    async def execute_work(self, state: ControllerState) -> None:
        work = self.bridge.materialize_work(state)
        runtime = self.bridge.runtime
        run = _latest_run(runtime, work.id)
        if run is None:
            run = runtime.start_run(work.id, workflow_id="personal-incident-repair")

        diagnosis_decisions = [
            decision
            for _event, decision in _ordered_decisions(self.controller, state.id)
            if decision.kind is ControllerDecisionKind.INVOKE_CAPABILITY
            and decision.parameters.get("phase") == "diagnosis"
        ]
        if not diagnosis_decisions:
            raise ValueError("materialized repair Work has no diagnosis")
        diagnosis = diagnosis_decisions[-1]
        diagnosis_result = _result_by_decision(self.controller, state.id).get(diagnosis.id)
        instruction = (
            (
                "Re-evaluate this fail-safe alert and provide a bounded human action plan. "
                "Do not modify files, call effect capabilities, access credentials, or claim "
                "recovery.\n\n"
                if self.fail_safe
                else "Execute the diagnosed repair now. Use the current repository only. Make the "
                "smallest necessary local changes and run relevant checks/tests. Do not push, "
                "merge, access remote credentials, or run Docker directly; remote/deployment "
                "effects belong to Agent Kernel providers. Finish with a concise execution "
                "summary.\n\n"
            )
            + "Diagnosis and plan:\n"
            + _message(diagnosis_result)
        )
        parameters: dict[str, Any] = {"model": self.execution_model, "phase": "execution"}
        if self.repo:
            parameters["repo"] = self.repo
        capability = "shell.exec" if self.repo else "reason.generate"
        result = await runtime.run_capability(
            work.id,
            capability,
            instruction=instruction,
            run_id=run.id,
            actor_ref=self.bridge.owner_principal,
            **parameters,
        )
        self.bridge.record_capability_result(
            controller_id=state.id,
            work_id=work.id,
            run_id=run.id,
            stage="execution",
            capability=capability,
            result=result,
        )
        if result.status != "succeeded":
            return

        if self.maintenance_capability:
            maintenance_parameters = {
                **self.maintenance_parameters,
                "phase": "apply",
            }
            applied = await runtime.run_capability(
                work.id,
                self.maintenance_capability,
                instruction="Apply the already approved bounded maintenance operation.",
                run_id=run.id,
                actor_ref=self.bridge.owner_principal,
                **maintenance_parameters,
            )
            self.bridge.record_capability_result(
                controller_id=state.id,
                work_id=work.id,
                run_id=run.id,
                stage="apply",
                capability=self.maintenance_capability,
                result=applied,
            )
            if applied.status != "succeeded":
                return

        if self.project:
            applied = await runtime.run_capability(
                work.id,
                "docker.compose.up",
                instruction="Apply the already prepared local repair for the configured project.",
                run_id=run.id,
                actor_ref=self.bridge.owner_principal,
                project=self.project,
                phase="apply",
            )
            self.bridge.record_capability_result(
                controller_id=state.id,
                work_id=work.id,
                run_id=run.id,
                stage="apply",
                capability="docker.compose.up",
                result=applied,
            )
            if applied.status != "succeeded":
                return

        verified = await runtime.run_capability(
            work.id,
            "monitor.alert.active",
            instruction="Verify whether the triggering Prometheus alert is still active.",
            run_id=run.id,
            actor_ref=self.bridge.owner_principal,
            labels=dict(self.verification_labels),
            phase="verification",
        )
        self.bridge.record_capability_result(
            controller_id=state.id,
            work_id=work.id,
            run_id=run.id,
            stage="verification",
            capability="monitor.alert.active",
            result=verified,
        )

    async def select(self, state: ControllerState) -> ControllerDecision:
        if state.status is ControllerStatus.CLOSED:
            raise ValueError("closed repair controller must not restart implicitly")
        if state.status is ControllerStatus.REOPEN_REQUIRED:
            return ControllerDecision(
                controller_ref=state.id,
                state_version=state.version,
                kind=ControllerDecisionKind.REOPEN,
                reason="RevisionAssessment explicitly requires renewed cognition",
            )
        if state.status is ControllerStatus.WAITING:
            if state.work_proposal_ref:
                if self._current_revision(state) is None:
                    return self._revision(state)
                if self.human_instruction:
                    return self._human_reopen_revision(state)
                raise ValueError("waiting repair controller requires an explicit owner command")
            return ControllerDecision(
                controller_ref=state.id,
                state_version=state.version,
                kind=ControllerDecisionKind.REOPEN,
                reason="explicitly resume a cognitive wait without handed-off Work",
            )

        if state.active_closure_ref:
            return self._propose_work(state)

        ordered = _ordered_decisions(self.controller, state.id)
        last = ordered[-1][1] if ordered else None
        if last is None or last.kind is ControllerDecisionKind.REOPEN:
            return self._diagnosis(state)
        if (
            last.kind is ControllerDecisionKind.INVOKE_CAPABILITY
            and last.capability == "reason.generate"
            and last.parameters.get("phase") == "diagnosis"
        ):
            result = _result_by_decision(self.controller, state.id).get(last.id)
            if not _succeeded(result):
                if self._diagnosis_count(state) < self.max_attempts:
                    return self._diagnosis(state, retry_context=_message(result))
                return ControllerDecision(
                    controller_ref=state.id,
                    state_version=state.version,
                    kind=ControllerDecisionKind.WAIT,
                    reason="the bounded diagnosis attempt budget is exhausted",
                )
            return self._form_closure(state, result)
        raise ValueError(f"unexpected open repair controller stage after {last.kind.value}")


@dataclass(frozen=True, slots=True)
class ManualTaskPolicy:
    """Explicit personal command routed through closure, Work and revision."""

    controller: CognitiveController = field(repr=False, compare=False)
    bridge: PersonalKernelBridge = field(repr=False, compare=False)
    prompt: str
    diagnosis_model: str
    execution_model: str
    repo: str | None = None
    human_instruction: str | None = None

    @property
    def policy_ref(self) -> str:
        return "control-plane/manual-task-v2"

    def _diagnosis(self, state: ControllerState) -> ControllerDecision:
        instruction = (
            "Interpret this explicit personal command, identify the concrete bounded work "
            "required, and give one execution instruction. Do not claim completion.\n\n"
            + self.prompt
        )
        if self.human_instruction:
            instruction += "\n\nExplicit owner follow-up:\n" + self.human_instruction
        parameters: dict[str, Any] = {"model": self.diagnosis_model, "phase": "diagnosis"}
        if self.repo:
            parameters["repo"] = self.repo
        return ControllerDecision(
            controller_ref=state.id,
            state_version=state.version,
            kind=ControllerDecisionKind.INVOKE_CAPABILITY,
            capability="reason.generate",
            instruction=instruction,
            parameters=parameters,
            reason="interpret the explicit personal command before cognitive closure",
        )

    def _execution_capability(self) -> str:
        return "shell.exec" if self.repo else "reason.generate"

    def _effect_class(self) -> EffectClass:
        return EffectClass.INTERNAL_REVERSIBLE if self.repo else EffectClass.READ_ONLY

    def _form_closure(
        self,
        state: ControllerState,
        result: dict[str, Any] | None,
    ) -> ControllerDecision:
        assessment_ref = self.bridge.assessment_ref(state)
        basis_refs = [assessment_ref]
        if state.last_result_ref:
            basis_refs.append(state.last_result_ref)
        closure = CognitiveClosure(
            controller_ref=state.id,
            controller_state_version=state.version,
            responsibility_ref=state.responsibility_ref,
            subject_ref=state.subject_ref,
            problem_ref=assessment_ref,
            basis_refs=basis_refs,
            selected_direction="execute the bounded explicit personal command",
            rationale=_message(result),
            acceptance_criteria=["the explicit personal task execution completes successfully"],
            verification_plan=["review the materialized Work provider result and embedded checks"],
            stop_conditions=["stop on provider failure and wait for owner direction"],
            escalation_conditions=["do not widen repository or effect scope implicitly"],
            reopen_conditions=["execution fails or the owner explicitly requests revision"],
            requested_capabilities=[self._execution_capability()],
            effect_class=self._effect_class(),
        )
        return ControllerDecision(
            controller_ref=state.id,
            state_version=state.version,
            kind=ControllerDecisionKind.FORM_CLOSURE,
            closure=closure,
            reason="form an explicit closure before creating personal Work",
        )

    def _propose_work(self, state: ControllerState) -> ControllerDecision:
        if not state.active_closure_ref:
            raise ValueError("manual task proposal requires active closure")
        context = self.bridge.context(state)
        return ControllerDecision(
            controller_ref=state.id,
            state_version=state.version,
            kind=ControllerDecisionKind.PROPOSE_WORK,
            closure_ref=state.active_closure_ref,
            assessment_ref=self.bridge.assessment_ref(state),
            work_kind="personal-command",
            work_title=context.title,
            work_description=context.description,
            requested_capabilities=[self._execution_capability()],
            expected_result="the explicit personal task execution completes successfully",
            stop_conditions=["stop on provider failure"],
            escalation_conditions=["wait for explicit owner direction before widening scope"],
            effect_class=self._effect_class().value,
            reason="handoff the personal command closure to WorkProposal",
        )

    def _current_revision(self, state: ControllerState) -> RevisionAssessment | None:
        if not state.last_revision_ref or not state.active_closure_ref:
            return None
        work = self.bridge.work_for_state(state)
        if work is None:
            return None
        return next(
            (
                revision
                for revision in self.controller.revisions(state.id)
                if revision.id == state.last_revision_ref
                and revision.closure_ref == state.active_closure_ref
                and revision.work_ref == work.id
            ),
            None,
        )

    def _revision(self, state: ControllerState) -> ControllerDecision:
        work = self.bridge.work_for_state(state)
        if work is None or not state.active_closure_ref:
            raise ValueError("manual revision requires materialized Work and active closure")
        events = self.bridge.result_events(state.id, work_id=work.id)
        if not events:
            raise ValueError("manual revision requires a durable Work result observation")
        result = _event_result(events[-1])
        succeeded = _succeeded(result)
        revision = RevisionAssessment(
            controller_ref=state.id,
            controller_state_version=state.version,
            work_ref=work.id,
            closure_ref=state.active_closure_ref,
            run_ref=_run_ref(events),
            outcome_refs=[event.id for event in events],
            verification_refs=[events[-1].id] if succeeded else [],
            reason_refs=[event.id for event in events],
            failure_class="" if succeeded else "execution-failure",
            revision_scope=RevisionScope.EXECUTION,
            recommended_disposition=(
                RevisionDisposition.CLOSE if succeeded else RevisionDisposition.WAIT
            ),
            reason=(
                "the bounded personal Work completed with its declared embedded checks"
                if succeeded
                else "the personal Work failed and requires explicit owner direction"
            ),
            carry_forward_refs=[self.bridge.assessment_ref(state)],
        )
        return ControllerDecision(
            controller_ref=state.id,
            state_version=state.version,
            kind=ControllerDecisionKind.ASSESS_REVISION,
            revision=revision,
            reason="return the materialized personal Work result through RevisionAssessment",
        )

    def _human_reopen_revision(self, state: ControllerState) -> ControllerDecision:
        work = self.bridge.work_for_state(state)
        if work is None or not state.active_closure_ref:
            raise ValueError("manual continuation requires materialized Work and active closure")
        events = self.bridge.result_events(state.id, work_id=work.id)
        if not events:
            raise ValueError("manual continuation has no prior Work result")
        instruction_event = self.bridge.latest_human_instruction_event(state.id)
        reason_refs = [event.id for event in events]
        if instruction_event is not None:
            reason_refs.append(instruction_event.id)
        revision = RevisionAssessment(
            controller_ref=state.id,
            controller_state_version=state.version,
            work_ref=work.id,
            closure_ref=state.active_closure_ref,
            run_ref=_run_ref(events),
            outcome_refs=[event.id for event in events],
            verification_refs=[],
            reason_refs=reason_refs,
            revision_scope=RevisionScope.DECISION,
            recommended_disposition=RevisionDisposition.REOPEN_COGNITION,
            reason="explicit owner follow-up requires a new bounded cognitive pass",
            carry_forward_refs=[self.bridge.assessment_ref(state)],
        )
        return ControllerDecision(
            controller_ref=state.id,
            state_version=state.version,
            kind=ControllerDecisionKind.ASSESS_REVISION,
            revision=revision,
            reason="reopen a waiting manual task through reality-grounded revision",
        )

    async def execute_work(self, state: ControllerState) -> None:
        work = self.bridge.materialize_work(state)
        runtime = self.bridge.runtime
        run = _latest_run(runtime, work.id)
        if run is None:
            run = runtime.start_run(work.id, workflow_id="personal-command")
        diagnoses = [
            decision
            for _event, decision in _ordered_decisions(self.controller, state.id)
            if decision.kind is ControllerDecisionKind.INVOKE_CAPABILITY
            and decision.parameters.get("phase") == "diagnosis"
        ]
        if not diagnoses:
            raise ValueError("materialized manual Work has no diagnosis")
        diagnosis = diagnoses[-1]
        result = _result_by_decision(self.controller, state.id).get(diagnosis.id)
        instruction = (
            "Execute this explicit personal task now within the available boundary. Run "
            "relevant checks where applicable and do not claim effects you cannot verify.\n\n"
            + _message(result)
        )
        parameters: dict[str, Any] = {"model": self.execution_model, "phase": "execution"}
        if self.repo:
            parameters["repo"] = self.repo
        capability = self._execution_capability()
        observed = await runtime.run_capability(
            work.id,
            capability,
            instruction=instruction,
            run_id=run.id,
            actor_ref=self.bridge.owner_principal,
            **parameters,
        )
        self.bridge.record_capability_result(
            controller_id=state.id,
            work_id=work.id,
            run_id=run.id,
            stage="execution",
            capability=capability,
            result=observed,
        )

    async def select(self, state: ControllerState) -> ControllerDecision:
        if state.status is ControllerStatus.CLOSED:
            raise ValueError("closed manual task must not restart implicitly")
        if state.status is ControllerStatus.REOPEN_REQUIRED:
            return ControllerDecision(
                controller_ref=state.id,
                state_version=state.version,
                kind=ControllerDecisionKind.REOPEN,
                reason="RevisionAssessment explicitly reopens the personal task",
            )
        if state.status is ControllerStatus.WAITING:
            if state.work_proposal_ref:
                if self._current_revision(state) is None:
                    return self._revision(state)
                if self.human_instruction:
                    return self._human_reopen_revision(state)
                raise ValueError("waiting manual task requires explicit owner direction")
            return ControllerDecision(
                controller_ref=state.id,
                state_version=state.version,
                kind=ControllerDecisionKind.REOPEN,
                reason="explicitly resume a cognitive wait without handed-off Work",
            )
        if state.active_closure_ref:
            return self._propose_work(state)

        ordered = _ordered_decisions(self.controller, state.id)
        last = ordered[-1][1] if ordered else None
        if last is None or last.kind is ControllerDecisionKind.REOPEN:
            return self._diagnosis(state)
        if (
            last.kind is ControllerDecisionKind.INVOKE_CAPABILITY
            and last.capability == "reason.generate"
            and last.parameters.get("phase") == "diagnosis"
        ):
            result = _result_by_decision(self.controller, state.id).get(last.id)
            if not _succeeded(result):
                return ControllerDecision(
                    controller_ref=state.id,
                    state_version=state.version,
                    kind=ControllerDecisionKind.WAIT,
                    reason="manual task diagnosis failed and requires owner attention",
                )
            return self._form_closure(state, result)
        raise ValueError(f"unexpected open manual task stage after {last.kind.value}")


async def drive_policy(
    controller: CognitiveController,
    controller_id: str,
    policy: AutonomousRepairPolicy | ManualTaskPolicy,
    *,
    max_steps: int = 32,
) -> ControllerState:
    """Drive one personal policy through the canonical Agent Kernel v2 loop."""

    state = controller.get(controller_id)
    if state is None:
        raise ValueError(f"unknown controller state: {controller_id}")

    for _ in range(max_steps):
        if state.status is ControllerStatus.CLOSED:
            return state

        if state.status is ControllerStatus.WAITING:
            if not state.work_proposal_ref:
                if policy.human_instruction:
                    state = await controller.step(state.id, policy)
                    continue
                return state

            work = policy.bridge.work_for_state(state)
            current_revision = policy._current_revision(state)
            if current_revision is not None:
                if policy.human_instruction:
                    state = await controller.step(state.id, policy)
                    continue
                return state

            if work is None or not policy.bridge.result_events(state.id, work_id=work.id):
                await policy.execute_work(state)
            state = await controller.step(state.id, policy)
            continue

        state = await controller.step(state.id, policy)

    raise RuntimeError("personal controller policy exceeded its bounded step budget")


UnattendedAlertPolicy = AutonomousRepairPolicy
