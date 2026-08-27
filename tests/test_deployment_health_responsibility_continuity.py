from __future__ import annotations

from datetime import UTC, datetime, timedelta

from control_plane.deployment_health_responsibility import DeploymentHealthResponsibilityProfile
from control_plane.models import Alert
from control_plane.verifier import CheckResult
from portable_runtime.responsibility import (
    DeploymentHealthState,
    EffectClass,
    ReasoningSessionBinding,
    ResponsibilityAssessment,
    ResponsibilityKernel,
    ResponsibilityStatus,
    WorkProposal,
)
from portable_runtime.stores.memory import InMemoryStateStore
from portable_runtime.stores.sqlite import SQLiteStateStore


def _t0() -> datetime:
    return datetime(2026, 8, 27, 8, 0, tzinfo=UTC)


def _firing_alert(now: datetime) -> Alert:
    return Alert(
        status="firing",
        labels={
            "alertname": "ControlPlaneHealthDegraded",
            "instance": "control-plane:8000",
            "project": "control-plane",
            "container": "control-plane",
        },
        annotations={"summary": "control-plane health is degraded"},
        startsAt=now - timedelta(minutes=1),
        generatorURL="http://prometheus/graph?g0.expr=up",
        fingerprint="fp-control-plane-health",
    )


def _resolved_alert(now: datetime) -> Alert:
    return Alert(
        status="resolved",
        labels={
            "alertname": "ControlPlaneHealthDegraded",
            "instance": "control-plane:8000",
            "project": "control-plane",
            "container": "control-plane",
        },
        annotations={"summary": "alert lifecycle reports resolved"},
        startsAt=now - timedelta(minutes=5),
        endsAt=now,
        generatorURL="http://prometheus/graph?g0.expr=up",
        fingerprint="fp-control-plane-health",
    )


def _admit(store, now: datetime) -> DeploymentHealthResponsibilityProfile:
    return DeploymentHealthResponsibilityProfile.admit(
        ResponsibilityKernel(store),
        responsibility_ref="sr_deployment_health",
        subject_ref="deployment:personal-platform:control-plane",
        scope={"profile": "personal-platform", "service": "control-plane"},
        principal_ref="principal:personal-platform-owner",
        now=now,
    )


def test_deployment_health_responsibility_survives_restart_and_rechecks_current_truth(tmp_path) -> None:
    t0 = _t0()
    db = tmp_path / "deployment-health-runtime.db"

    # Session A sees a real control-plane Alert object and records only a
    # durable assessment + read-only diagnostic proposal. No Work or authority
    # is admitted by this profile adapter.
    first_store = SQLiteStateStore(db)
    first_profile = _admit(first_store, t0)
    session_a = first_profile.bind_reasoning_session(
        provider="openai",
        model="model-a",
        session_ref="reasoning-session-a",
        now=t0,
    )
    alert_cycle = first_profile.observe_alert(_firing_alert(t0), now=t0)

    assert alert_cycle.state is DeploymentHealthState.ALERT_ACTIVE
    assert alert_cycle.proposal is not None
    assert alert_cycle.proposal.effect_class is EffectClass.READ_ONLY
    assert first_profile.current_proposal(now=t0) == alert_cycle.proposal
    assert first_store.list_work() == []
    assert first_store.list_authorizations() == []

    snapshot = first_profile.checkpoint_session(
        session_a.id,
        evidence_refs=list(alert_cycle.assessment.basis_refs),
    )
    old_assessment_id = alert_cycle.assessment.id
    old_proposal_id = alert_cycle.proposal.id
    old_proposal_fresh_until = alert_cycle.proposal.fresh_until
    first_store.close()

    # A new process reopens the same SQLite journal. The reasoning provider,
    # model, and session all change, but responsibility identity does not.
    t1 = t0 + timedelta(minutes=2)
    reopened_store = SQLiteStateStore(db)
    reopened_kernel = ResponsibilityKernel(reopened_store)
    second_profile = DeploymentHealthResponsibilityProfile.reopen(
        reopened_kernel,
        responsibility_ref="sr_deployment_health",
        subject_ref="deployment:personal-platform:control-plane",
    )
    handoff = second_profile.resume_from_checkpoint(
        from_binding_ref=session_a.id,
        snapshot_ref=snapshot.id,
        provider="anthropic",
        model="model-b",
        session_ref="reasoning-session-b",
        now=t1,
    )

    assert handoff.validation.continuity_ok is True
    assert handoff.validation.authorization_revalidation_required is True
    assert handoff.binding.provider != session_a.provider
    assert handoff.binding.model != session_a.model
    assert handoff.binding.session_ref != session_a.session_ref
    assert handoff.handoff.from_session_ref == session_a.id
    assert handoff.handoff.to_session_ref == handoff.binding.id
    assert reopened_kernel.get_responsibility("sr_deployment_health").id == "sr_deployment_health"
    assert reopened_kernel.current_status("sr_deployment_health") is ResponsibilityStatus.ACTIVE

    # The old proposal is deliberately still fresh. It remains the current
    # historical proposal until session B obtains a newer operational fact.
    assert old_proposal_fresh_until is not None
    assert t1 < old_proposal_fresh_until
    current_before_refresh = second_profile.current_proposal(now=t1)
    assert current_before_refresh is not None
    assert current_before_refresh.id == old_proposal_id

    # Fresh Prometheus evidence now says the service is healthy. This new fact
    # supersedes the historical unhealthy assessment for current-use purposes;
    # it does not delete history and it does not discharge the responsibility.
    healthy_cycle = second_profile.observe_prometheus_check(
        CheckResult(
            name="promql:control_plane_health",
            passed=True,
            message="fresh Prometheus read confirms current deployment health",
            evidence_ref="evidence:prometheus:control-plane:healthy:t1",
        ),
        now=t1 + timedelta(seconds=1),
    )

    assert healthy_cycle.state is DeploymentHealthState.HEALTH_VERIFIED
    assert healthy_cycle.proposal is None
    assert second_profile.current_assessment() == healthy_cycle.assessment
    assert second_profile.current_proposal(now=t1 + timedelta(seconds=1)) is None

    assessments = reopened_kernel.journal.list("ResponsibilityAssessment", "sr_deployment_health")
    proposals = reopened_kernel.journal.list("WorkProposal", "sr_deployment_health")
    assert any(isinstance(value, ResponsibilityAssessment) and value.id == old_assessment_id for value in assessments)
    assert any(isinstance(value, WorkProposal) and value.id == old_proposal_id for value in proposals)
    assert len(assessments) == 2
    assert len(proposals) == 1

    # Changed current facts block historical proposal use without relying on
    # expiry: no admission, commitment, Work, or execution authority appears.
    assert reopened_kernel.journal.list("PriorityJudgment", "sr_deployment_health") == []
    assert reopened_kernel.journal.list("PortfolioAdmissionDecision", "sr_deployment_health") == []
    assert reopened_kernel.journal.list("Commitment", "sr_deployment_health") == []
    assert reopened_store.list_work() == []
    assert reopened_store.list_authorizations() == []
    assert reopened_kernel.current_status("sr_deployment_health") is ResponsibilityStatus.ACTIVE

    bindings = reopened_kernel.journal.list("ReasoningSessionBinding", "sr_deployment_health")
    assert len(bindings) == 2
    assert all(isinstance(value, ReasoningSessionBinding) for value in bindings)
    reopened_store.close()


def test_resolved_alert_and_unbound_prometheus_success_do_not_verify_health() -> None:
    now = _t0()
    store = InMemoryStateStore()
    profile = _admit(store, now)

    resolved_cycle = profile.observe_alert(_resolved_alert(now), now=now)
    assert resolved_cycle.state is DeploymentHealthState.RECOVERY_NOT_VERIFIED
    assert resolved_cycle.proposal is not None
    assert resolved_cycle.proposal.effect_class is EffectClass.READ_ONLY

    unbound_success = profile.observe_prometheus_check(
        CheckResult(
            name="promql:control_plane_health",
            passed=True,
            message="query returned success but no durable evidence reference was bound",
            evidence_ref="",
        ),
        now=now + timedelta(seconds=1),
    )
    assert unbound_success.state is DeploymentHealthState.RECOVERY_NOT_VERIFIED
    assert unbound_success.proposal is not None
    assert profile.current_assessment() == unbound_success.assessment

    states = [
        value.assessment_kind
        for value in profile.kernel.journal.list("ResponsibilityAssessment", "sr_deployment_health")
        if isinstance(value, ResponsibilityAssessment)
    ]
    assert "deployment-health:health-verified" not in states
    assert store.list_work() == []
    assert store.list_authorizations() == []
    assert profile.kernel.current_status("sr_deployment_health") is ResponsibilityStatus.ACTIVE
