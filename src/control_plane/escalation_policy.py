from __future__ import annotations

from dataclasses import dataclass, field
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
from portable_runtime.core.capabilities import CapabilityResult
from portable_runtime.core.models import new_id, utcnow
from portable_runtime.responsibility import EffectClass

from .kernel_bridge import PersonalKernelBridge


@dataclass(frozen=True, slots=True)
class BlockedEscalationPolicy:
    """Materialize a no-effect Work when cognition itself cannot proceed.

    This preserves the personal contract that every accepted task/incident has a
    durable Work id without recreating the old ingress-created Work shortcut.
    The failed cognitive result is carried through an explicit closure/proposal,
    then attached to the materialized Work as reality before RevisionAssessment
    puts the controller back into WAITING.
    """

    controller: CognitiveController = field(repr=False, compare=False)
    bridge: PersonalKernelBridge = field(repr=False, compare=False)
    failed_result: dict[str, Any] | None = field(default=None, repr=False, compare=False)

    @property
    def policy_ref(self) -> str:
        return "control-plane/blocked-escalation-v2"

    def _closure(self, state: ControllerState) -> ControllerDecision:
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
                "preserve the blocked personal task durably and wait for owner direction"
            ),
            rationale=str((self.failed_result or {}).get("message", "cognitive step failed")),
            acceptance_criteria=[
                "the blocked task has a durable Work identity for owner follow-up"
            ],
            verification_plan=["bind the observed cognitive failure to the materialized Work"],
            stop_conditions=["do not execute repair or task effects from a failed diagnosis"],
            escalation_conditions=["wait for an explicit owner command"],
            reopen_conditions=["the owner supplies an explicit continuation command"],
            requested_capabilities=[],
            effect_class=EffectClass.READ_ONLY,
        )
        return ControllerDecision(
            controller_ref=state.id,
            state_version=state.version,
            kind=ControllerDecisionKind.FORM_CLOSURE,
            closure=closure,
            reason="capture cognitive failure as a bounded no-effect closure",
        )

    def _proposal(self, state: ControllerState) -> ControllerDecision:
        if not state.active_closure_ref:
            raise ValueError("blocked escalation requires an active cognitive closure")
        context = self.bridge.context(state)
        return ControllerDecision(
            controller_ref=state.id,
            state_version=state.version,
            kind=ControllerDecisionKind.PROPOSE_WORK,
            closure_ref=state.active_closure_ref,
            assessment_ref=self.bridge.assessment_ref(state),
            work_kind=f"{context.kind}-blocked",
            work_title=context.title,
            work_description=context.description,
            requested_capabilities=[],
            expected_result="await explicit owner direction without executing effects",
            stop_conditions=["no task or repair effect may execute from this blocked proposal"],
            escalation_conditions=["owner follow-up may reopen cognition explicitly"],
            effect_class=EffectClass.READ_ONLY.value,
            reason="materialize a durable no-effect Work for the blocked personal responsibility",
        )

    def _revision(self, state: ControllerState) -> ControllerDecision:
        work = self.bridge.work_for_state(state)
        if work is None or not state.active_closure_ref:
            raise ValueError("blocked escalation revision requires materialized Work and closure")
        events = self.bridge.result_events(state.id, work_id=work.id)
        if not events:
            raise ValueError(
                "blocked escalation revision requires a Work-bound failure observation"
            )
        event = events[-1]
        revision = RevisionAssessment(
            controller_ref=state.id,
            controller_state_version=state.version,
            work_ref=work.id,
            closure_ref=state.active_closure_ref,
            run_ref=str(event.payload.get("run_ref") or "") or None,
            outcome_refs=[event.id],
            verification_refs=[],
            reason_refs=[event.id],
            failure_class="diagnosis-failure",
            revision_scope=RevisionScope.DECISION,
            recommended_disposition=RevisionDisposition.WAIT,
            reason=(
                "cognition failed before safe effect execution; preserve Work and wait for "
                "owner direction"
            ),
            carry_forward_refs=[self.bridge.assessment_ref(state)],
        )
        return ControllerDecision(
            controller_ref=state.id,
            state_version=state.version,
            kind=ControllerDecisionKind.ASSESS_REVISION,
            revision=revision,
            reason="return the cognitive blocker through the canonical RevisionAssessment path",
        )

    def _capability_result(self) -> CapabilityResult:
        if self.failed_result is not None:
            return CapabilityResult.model_validate(self.failed_result)
        return CapabilityResult(
            request_id=new_id("request"),
            provider_id="control-plane",
            status="failed",
            message="cognitive step failed before a bounded execution plan was available",
        )

    def bind_failure_to_work(self, state: ControllerState) -> None:
        work = self.bridge.materialize_work(state)
        if self.bridge.result_events(state.id, work_id=work.id):
            return
        run = self.bridge.runtime.start_run(work.id, workflow_id="personal-blocked-escalation")
        self.bridge.record_capability_result(
            controller_id=state.id,
            work_id=work.id,
            run_id=run.id,
            stage="cognitive-blocker",
            capability="reason.generate",
            result=self._capability_result(),
        )
        # ``start_run`` is only used here to bind durable evidence. It is not
        # a live execution after the failure has been recorded. Close that
        # execution claim explicitly and leave the Work waiting for an owner.
        self.bridge.runtime.store.save_run(
            run.model_copy(update={"status": "interrupted", "ended_at": utcnow()})
        )
        self.bridge.runtime.store.save_work(
            work.model_copy(update={"status": "waiting", "updated_at": utcnow()})
        )

    async def select(self, state: ControllerState) -> ControllerDecision:
        if state.status is ControllerStatus.WAITING and not state.work_proposal_ref:
            return ControllerDecision(
                controller_ref=state.id,
                state_version=state.version,
                kind=ControllerDecisionKind.REOPEN,
                reason="reopen only to form a durable no-effect escalation closure",
            )
        if state.status is ControllerStatus.OPEN and not state.active_closure_ref:
            return self._closure(state)
        if state.status is ControllerStatus.OPEN and state.active_closure_ref:
            return self._proposal(state)
        if state.status is ControllerStatus.WAITING and state.work_proposal_ref:
            return self._revision(state)
        raise ValueError(f"blocked escalation cannot handle controller status {state.status.value}")


async def preserve_blocked_wait(
    controller: CognitiveController,
    bridge: PersonalKernelBridge,
    state: ControllerState,
) -> ControllerState:
    """Give a pre-Work WAITING controller a durable Work id without any effect execution."""

    if state.status is not ControllerStatus.WAITING or state.work_proposal_ref:
        return state
    policy = BlockedEscalationPolicy(
        controller=controller,
        bridge=bridge,
        failed_result=bridge.latest_result(state.id),
    )
    state = await controller.step(state.id, policy)
    state = await controller.step(state.id, policy)
    state = await controller.step(state.id, policy)
    policy.bind_failure_to_work(state)
    state = await controller.step(state.id, policy)
    if state.status is not ControllerStatus.WAITING or bridge.work_for_state(state) is None:
        raise RuntimeError("blocked personal task did not settle into durable WAITING Work")
    return state
