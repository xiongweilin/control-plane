from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from portable_runtime.controller import CognitiveController, ControllerState
from portable_runtime.core.capabilities import CapabilityResult
from portable_runtime.core.models import Event, Work, new_id, utcnow
from portable_runtime.responsibility import (
    Commitment,
    EffectClass,
    PortfolioAdmissionDecision,
    PriorityDimensions,
    PriorityJudgment,
    ResourcePool,
    ResourceReservation,
    ResourceVector,
    ResponsibilityAdmission,
    ResponsibilityExpectation,
    ResponsibilityKernel,
    StandingResponsibility,
    WorkProposal,
)

PERSONAL_RESULT_EVENT = "PersonalWorkCapabilityResultObserved"
PERSONAL_HUMAN_INSTRUCTION_EVENT = "PersonalHumanInstructionObserved"
PERSONAL_ADMISSION_POLICY = "control-plane/personal-admission-v2"
PERSONAL_RESOURCE_POOL_ID = "control_plane_personal_pool_v2"


@dataclass(frozen=True, slots=True)
class PersonalResponsibilityContext:
    title: str
    description: str
    kind: str
    repo: str | None
    project: str | None
    verification_labels: dict[str, str]


class PersonalKernelBridge:
    """Thin personal-profile adapter over Agent Kernel's durable core semantics.

    The bridge owns no alternative Work/controller state machine. It only maps
    personal task context into the kernel's StandingResponsibility and portfolio
    primitives, then records provider results as profile observations for the
    controller's RevisionAssessment path.
    """

    def __init__(
        self,
        runtime: Any,
        controller: CognitiveController,
        *,
        owner_principal: str,
    ) -> None:
        self.runtime = runtime
        self.controller = controller
        self.responsibilities = ResponsibilityKernel(runtime.store)
        self.owner_principal = owner_principal

    def begin(
        self,
        *,
        title: str,
        description: str,
        kind: str,
        repo: str | None = None,
        project: str | None = None,
        verification_labels: dict[str, str] | None = None,
        parent_controller_id: str | None = None,
    ) -> tuple[ControllerState, str]:
        responsibility = StandingResponsibility(
            responsibility_kind=kind,
            statement=description,
            scope={
                "profile": "control-plane",
                "title": title,
                "kind": kind,
                "repo": repo or "",
                "project": project or "",
                "verification_labels": json.dumps(
                    dict(verification_labels or {}),
                    ensure_ascii=False,
                    sort_keys=True,
                ),
            },
        )
        self.responsibilities.register(
            responsibility,
            ResponsibilityAdmission(
                responsibility_ref=responsibility.id,
                responsibility_version=1,
                principal_ref=self.owner_principal,
                basis_refs=["control-plane:explicit-personal-responsibility"],
            ),
        )

        now = utcnow()
        subject_ref = f"personal:{responsibility.id}"
        expectation = ResponsibilityExpectation(
            responsibility_ref=responsibility.id,
            responsibility_version=1,
            subject_ref=subject_ref,
            expected_signal_kind=(
                "alert-cleared" if kind == "personal-incident-repair" else "task-completion"
            ),
            due_at=now - timedelta(microseconds=1),
        )
        self.responsibilities.create_expectation(expectation)
        assessment = self.responsibilities.assess_due_expectation(
            expectation.id,
            now=now,
            observed_evidence_refs=[],
        )
        if assessment is None:
            raise RuntimeError("personal responsibility did not produce an actionable assessment")

        context_refs = [assessment.id, expectation.id]
        if parent_controller_id:
            context_refs.append(parent_controller_id)
        state = self.controller.create(
            responsibility_ref=responsibility.id,
            subject_ref=subject_ref,
            context_refs=context_refs,
        )
        return state, assessment.id

    def context(self, state: ControllerState) -> PersonalResponsibilityContext:
        if not state.responsibility_ref:
            raise ValueError("personal controller has no standing responsibility")
        responsibility = self.responsibilities.get_responsibility(state.responsibility_ref)
        scope = dict(responsibility.scope)
        raw_labels = scope.get("verification_labels", "{}")
        try:
            parsed = json.loads(raw_labels)
        except json.JSONDecodeError:
            parsed = {}
        labels = (
            {str(key): str(value) for key, value in parsed.items()}
            if isinstance(parsed, dict)
            else {}
        )
        return PersonalResponsibilityContext(
            title=scope.get("title", "Personal task"),
            description=responsibility.statement,
            kind=scope.get("kind", responsibility.responsibility_kind),
            repo=scope.get("repo") or None,
            project=scope.get("project") or None,
            verification_labels=labels,
        )

    def assessment_ref(self, state: ControllerState) -> str:
        for ref in state.context_refs:
            value = self.responsibilities.journal.get(ref)
            if value is not None and value.object_type == "ResponsibilityAssessment":
                return str(ref)
        raise ValueError("personal controller has no responsibility assessment")

    def work_for_proposal(self, proposal_ref: str) -> Work | None:
        for work in self.runtime.list_work():
            metadata = work.metadata if isinstance(work.metadata, dict) else {}
            if metadata.get("responsibility_proposal_ref") == proposal_ref:
                return work
        return None

    def work_for_state(self, state: ControllerState) -> Work | None:
        if not state.work_proposal_ref:
            return None
        return self.work_for_proposal(state.work_proposal_ref)

    def materialize_work(self, state: ControllerState) -> Work:
        proposal_ref = state.work_proposal_ref
        if not proposal_ref:
            raise ValueError("controller has no handed-off WorkProposal")
        existing = self.work_for_proposal(proposal_ref)
        if existing is not None:
            return existing

        raw = self.responsibilities.journal.get(proposal_ref)
        if not isinstance(raw, WorkProposal):
            raise ValueError("controller WorkProposal is unavailable")
        proposal = raw
        risk = 3 if proposal.effect_class is EffectClass.EXTERNAL_EFFECT else 1
        reversibility = 2 if proposal.effect_class is EffectClass.EXTERNAL_EFFECT else 4

        priority = PriorityJudgment(
            id=f"priority_{proposal.id}",
            proposal_ref=proposal.id,
            dimensions=PriorityDimensions(
                urgency=4,
                impact=4,
                risk=risk,
                reversibility=reversibility,
                confidence=4,
                resource_cost=1,
                human_attention_cost=0,
            ),
            policy_ref=PERSONAL_ADMISSION_POLICY,
            admitted=True,
            rationale="explicit owner task is inside the bounded personal control-plane profile",
        )
        self.responsibilities.record_priority_judgment(priority)

        pool = ResourcePool(
            id=PERSONAL_RESOURCE_POOL_ID,
            pool_key="personal-control-plane",
            capacity=ResourceVector(
                compute_units=8,
                api_calls=64,
                human_attention_units=4,
                concurrency_slots=8,
            ),
            policy_ref=PERSONAL_ADMISSION_POLICY,
        )
        if self.responsibilities.journal.get(pool.id) is None:
            self.responsibilities.create_resource_pool(pool)

        portfolio = PortfolioAdmissionDecision(
            id=f"portfolio_{proposal.id}",
            proposal_ref=proposal.id,
            resource_pool_ref=pool.id,
            policy_ref=PERSONAL_ADMISSION_POLICY,
            admitted=True,
            rationale="personal profile capacity is available for this explicit bounded proposal",
        )
        self.responsibilities.record_portfolio_admission(portfolio)

        reservation = ResourceReservation(
            id=f"reservation_{proposal.id}",
            responsibility_ref=proposal.responsibility_ref,
            proposal_ref=proposal.id,
            resource_pool_ref=pool.id,
            resources=proposal.requested_resources,
            reserved_at=proposal.created_at,
        )
        self.responsibilities.reserve(reservation, now=utcnow())

        commitment = Commitment(
            id=f"commitment_{proposal.id}",
            responsibility_ref=proposal.responsibility_ref,
            responsibility_version=proposal.responsibility_version,
            proposal_ref=proposal.id,
            priority_judgment_ref=priority.id,
            portfolio_admission_ref=portfolio.id,
            reservation_ref=reservation.id,
            resources=proposal.requested_resources,
            committed_at=proposal.created_at,
            stop_conditions=list(proposal.stop_conditions),
            escalation_conditions=list(proposal.escalation_conditions),
        )
        self.responsibilities.commit(commitment, now=utcnow())
        return self.responsibilities.materialize_work(commitment.id)

    def record_capability_result(
        self,
        *,
        controller_id: str,
        work_id: str,
        run_id: str,
        stage: str,
        capability: str,
        result: CapabilityResult,
    ) -> Event:
        event = Event(
            id=new_id("event"),
            type=PERSONAL_RESULT_EVENT,
            subject_ref=controller_id,
            payload={
                "work_ref": work_id,
                "run_ref": run_id,
                "stage": stage,
                "capability": capability,
                "result": result.model_dump(mode="json"),
            },
        )
        self.runtime.store.append_event(event)
        return event

    def result_events(self, controller_id: str, *, work_id: str | None = None) -> list[Event]:
        values = [
            event
            for event in self.runtime.store.list_events(controller_id)
            if event.type == PERSONAL_RESULT_EVENT
            and (work_id is None or event.payload.get("work_ref") == work_id)
        ]
        return sorted(values, key=lambda event: event.created_at)

    def latest_result(self, controller_id: str) -> dict[str, Any] | None:
        events = self.result_events(controller_id)
        if events:
            raw = events[-1].payload.get("result")
            if isinstance(raw, dict):
                return dict(raw)

        core_results = [
            event
            for event in self.runtime.store.list_events(controller_id)
            if event.type == "ControllerCapabilityResultObserved"
        ]
        if not core_results:
            return None
        event = max(core_results, key=lambda value: value.created_at)
        raw = event.payload.get("result")
        return dict(raw) if isinstance(raw, dict) else None

    def latest_human_instruction_event(self, controller_id: str) -> Event | None:
        values = [
            event
            for event in self.runtime.store.list_events(controller_id)
            if event.type == PERSONAL_HUMAN_INSTRUCTION_EVENT
        ]
        return max(values, key=lambda event: event.created_at) if values else None
