from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta

from portable_runtime.responsibility import (
    ContinuityValidation,
    DeploymentHealthState,
    ReasoningSessionBinding,
    ResponsibilityAdmission,
    ResponsibilityAssessment,
    ResponsibilityContextSnapshot,
    ResponsibilityHandoff,
    ResponsibilityKernel,
    ResponsibilityStatus,
    StandingResponsibility,
    WorkProposal,
    deployment_health_proposal,
    record_domain_assessment,
)

from .models import Alert
from .verifier import CheckResult


def _stable_id(prefix: str, *parts: object) -> str:
    payload = "\x1f".join(str(part) for part in parts).encode("utf-8")
    return f"{prefix}_{hashlib.sha256(payload).hexdigest()[:32]}"


@dataclass(frozen=True, slots=True)
class DeploymentHealthCycle:
    state: DeploymentHealthState
    assessment: ResponsibilityAssessment
    proposal: WorkProposal | None


@dataclass(frozen=True, slots=True)
class DeploymentHealthHandoffResult:
    binding: ReasoningSessionBinding
    handoff: ResponsibilityHandoff
    validation: ContinuityValidation


class DeploymentHealthResponsibilityProfile:
    """Profile-owned operational facts over the portable responsibility kernel.

    This adapter interprets control-plane Alertmanager and deterministic
    Prometheus verification facts. It may record a canonical assessment and a
    read-only diagnostic proposal. It deliberately has no portfolio admission,
    commitment, Work materialization, authorization, or repair execution API.

    In particular, an Alertmanager ``resolved`` event and an unbound boolean
    Prometheus success are not evidence of verified health.
    """

    ASSESSMENT_FRESHNESS = timedelta(minutes=10)

    def __init__(
        self,
        kernel: ResponsibilityKernel,
        *,
        responsibility_ref: str,
        subject_ref: str,
    ) -> None:
        self.kernel = kernel
        self.responsibility_ref = responsibility_ref
        self.subject_ref = subject_ref

    @classmethod
    def admit(
        cls,
        kernel: ResponsibilityKernel,
        *,
        responsibility_ref: str,
        subject_ref: str,
        scope: dict[str, str],
        principal_ref: str,
        now: datetime,
    ) -> DeploymentHealthResponsibilityProfile:
        identity = StandingResponsibility(
            id=responsibility_ref,
            responsibility_kind="deployment-health",
            statement="Maintain deployment health for the declared profile and service scope",
            scope=dict(scope),
            created_at=now,
        )
        admission = ResponsibilityAdmission(
            id=_stable_id("deployment_health_admission", responsibility_ref, principal_ref),
            responsibility_ref=responsibility_ref,
            responsibility_version=1,
            principal_ref=principal_ref,
            basis_refs=["profile-policy:deployment-health-v1"],
            created_at=now,
            admitted_at=now,
        )
        kernel.register(identity, admission)
        return cls(kernel, responsibility_ref=responsibility_ref, subject_ref=subject_ref)

    @classmethod
    def reopen(
        cls,
        kernel: ResponsibilityKernel,
        *,
        responsibility_ref: str,
        subject_ref: str,
    ) -> DeploymentHealthResponsibilityProfile:
        identity = kernel.get_responsibility(responsibility_ref)
        if identity.responsibility_kind != "deployment-health":
            raise ValueError("standing responsibility is not deployment-health")
        if kernel.current_status(responsibility_ref) is not ResponsibilityStatus.ACTIVE:
            raise ValueError("deployment-health responsibility is not active")
        return cls(kernel, responsibility_ref=responsibility_ref, subject_ref=subject_ref)

    def _record_state(
        self,
        *,
        state: DeploymentHealthState,
        basis_refs: list[str],
        now: datetime,
        source_kind: str,
        rationale: str,
    ) -> DeploymentHealthCycle:
        version, _statement, _scope = self.kernel.current_definition(self.responsibility_ref)
        assessment = ResponsibilityAssessment(
            id=_stable_id(
                "deployment_health_assessment",
                self.responsibility_ref,
                self.subject_ref,
                state.value,
                now.isoformat(),
                *basis_refs,
            ),
            responsibility_ref=self.responsibility_ref,
            responsibility_version=version,
            subject_ref=self.subject_ref,
            assessment_kind=f"deployment-health:{state.value}",
            basis_refs=list(basis_refs),
            assessed_at=now,
            fresh_until=now + self.ASSESSMENT_FRESHNESS,
            rationale=f"{source_kind}: {rationale}",
            created_at=now,
        )
        recorded = record_domain_assessment(self.kernel, assessment, now=now)
        proposal = deployment_health_proposal(recorded, state=state, now=now)
        if proposal is not None:
            proposal = self.kernel.propose(proposal, now=now)
        return DeploymentHealthCycle(state=state, assessment=recorded, proposal=proposal)

    def observe_alert(self, alert: Alert, *, now: datetime) -> DeploymentHealthCycle:
        if alert.status == "firing":
            state = DeploymentHealthState.ALERT_ACTIVE
            rationale = "current Alertmanager fact is firing"
        else:
            # Alert lifecycle state is not a recovery proof. Existing
            # control-plane recovery verification remains the fact owner for
            # any later verified-healthy assessment.
            state = DeploymentHealthState.RECOVERY_NOT_VERIFIED
            rationale = "Alertmanager reports resolved, but recovery has not been independently verified"
        observed_at = alert.ends_at if alert.status == "resolved" and alert.ends_at is not None else now
        fact_ref = _stable_id(
            "alertmanager_fact",
            alert.fingerprint,
            alert.status,
            alert.starts_at.isoformat(),
            observed_at.isoformat(),
        )
        return self._record_state(
            state=state,
            basis_refs=[f"alertmanager:{fact_ref}"],
            now=now,
            source_kind="alertmanager",
            rationale=rationale,
        )

    def observe_prometheus_check(self, check: CheckResult, *, now: datetime) -> DeploymentHealthCycle:
        if not check.name.startswith("promql:"):
            raise ValueError("deployment-health Prometheus observation requires a promql check")
        has_bound_evidence = bool(check.evidence_ref.strip())
        if check.passed and has_bound_evidence:
            state = DeploymentHealthState.HEALTH_VERIFIED
            rationale = "fresh deterministic Prometheus check passed with bound evidence"
        else:
            state = DeploymentHealthState.RECOVERY_NOT_VERIFIED
            rationale = (
                "Prometheus check did not prove current health"
                if not check.passed
                else "Prometheus boolean success had no bound evidence and cannot verify current health"
            )
        basis_refs = [check.evidence_ref] if has_bound_evidence else []
        return self._record_state(
            state=state,
            basis_refs=basis_refs,
            now=now,
            source_kind=check.name,
            rationale=rationale,
        )

    def current_assessment(self) -> ResponsibilityAssessment | None:
        assessments = [
            value
            for value in self.kernel.journal.list("ResponsibilityAssessment", self.responsibility_ref)
            if isinstance(value, ResponsibilityAssessment)
            and value.subject_ref == self.subject_ref
            and value.assessment_kind.startswith("deployment-health:")
        ]
        if not assessments:
            return None
        return max(assessments, key=lambda value: (value.assessed_at, value.id))

    def current_proposal(self, *, now: datetime) -> WorkProposal | None:
        """Return only a proposal bound to the latest current assessment.

        Historical proposals stay in the append-only journal, but a fresh
        current fact supersedes their eligibility even when their own TTL has
        not expired. This is a read-only profile projection, not admission.
        """

        assessment = self.current_assessment()
        if assessment is None:
            return None
        version, _statement, _scope = self.kernel.current_definition(self.responsibility_ref)
        proposals = [
            value
            for value in self.kernel.journal.list("WorkProposal", self.responsibility_ref)
            if isinstance(value, WorkProposal)
            and value.subject_ref == self.subject_ref
            and value.assessment_ref == assessment.id
            and value.responsibility_version == version
            and (value.fresh_until is None or now <= value.fresh_until)
        ]
        if not proposals:
            return None
        return max(proposals, key=lambda value: (value.created_at, value.id))

    def bind_reasoning_session(
        self,
        *,
        provider: str,
        model: str,
        session_ref: str,
        now: datetime,
    ) -> ReasoningSessionBinding:
        version, _statement, _scope = self.kernel.current_definition(self.responsibility_ref)
        binding = ReasoningSessionBinding(
            id=_stable_id(
                "deployment_health_session",
                self.responsibility_ref,
                version,
                provider,
                model,
                session_ref,
            ),
            responsibility_ref=self.responsibility_ref,
            responsibility_version=version,
            provider=provider,
            model=model,
            session_ref=session_ref,
            started_at=now,
            created_at=now,
        )
        return self.kernel.bind_reasoning_session(binding)

    def checkpoint_session(
        self,
        binding_ref: str,
        *,
        evidence_refs: list[str] | None = None,
    ) -> ResponsibilityContextSnapshot:
        binding = self.kernel.journal.get(binding_ref)
        if not isinstance(binding, ReasoningSessionBinding):
            raise ValueError("checkpoint requires a reasoning session binding")
        if binding.responsibility_ref != self.responsibility_ref:
            raise ValueError("reasoning session belongs to another responsibility")
        return self.kernel.create_context_snapshot(
            self.responsibility_ref,
            evidence_refs=list(evidence_refs or []),
            reopen_conditions=["re-read current monitoring facts after restart or context replacement"],
            stop_conditions=["do not admit historical proposal without current-fact revalidation"],
            escalation_conditions=["external mutation requires separate current authorization"],
        )

    def resume_from_checkpoint(
        self,
        *,
        from_binding_ref: str,
        snapshot_ref: str,
        provider: str,
        model: str,
        session_ref: str,
        now: datetime,
    ) -> DeploymentHealthHandoffResult:
        binding = self.bind_reasoning_session(
            provider=provider,
            model=model,
            session_ref=session_ref,
            now=now,
        )
        version, _statement, _scope = self.kernel.current_definition(self.responsibility_ref)
        handoff = ResponsibilityHandoff(
            id=_stable_id(
                "deployment_health_handoff",
                self.responsibility_ref,
                version,
                from_binding_ref,
                binding.id,
                snapshot_ref,
            ),
            responsibility_ref=self.responsibility_ref,
            responsibility_version=version,
            from_session_ref=from_binding_ref,
            to_session_ref=binding.id,
            context_snapshot_ref=snapshot_ref,
            handed_off_at=now,
            created_at=now,
        )
        recorded_handoff = self.kernel.handoff(handoff)
        validation = self.kernel.validate_handoff(recorded_handoff.id, now=now)
        return DeploymentHealthHandoffResult(
            binding=binding,
            handoff=recorded_handoff,
            validation=validation,
        )


__all__ = [
    "DeploymentHealthCycle",
    "DeploymentHealthHandoffResult",
    "DeploymentHealthResponsibilityProfile",
]
