from __future__ import annotations

from meta_controller import (
    EpistemicIssueKind,
    EpistemicMode,
    EpistemicState,
    MetaControlIntentKind,
    MetaControllerEngine,
    StructuralTensionKind,
)

from control_plane.epistemic_profile import (
    build_repair_epistemic_profile,
    render_meta_control_directive,
)


def _projection(controller_ref: str = "controller:test", version: int = 1) -> EpistemicState:
    return EpistemicState(
        controller_ref=controller_ref,
        state_version=version,
        mode=EpistemicMode.OPEN_EXPLORATION,
        candidate_count=0,
        open_issue_count=0,
        has_result=False,
        has_closure=False,
        has_work=False,
        has_revision=False,
        tags=frozenset({EpistemicMode.OPEN_EXPLORATION.value}),
    )


def _frame(*, attempt: int, line_endings: bool = False, retry_context: str = ""):
    projection = _projection(version=attempt)
    profile = build_repair_epistemic_profile(
        controller_ref=projection.controller_ref,
        state_version=projection.state_version,
        attempt=attempt,
        attempt_limit=2,
        is_line_ending_cleanup=line_endings,
        has_repo=True,
        has_project=False,
        has_maintenance_capability=line_endings,
        retry_context=retry_context,
    )
    frame = MetaControllerEngine().evaluate(
        projection,
        issues=profile.issues,
        tensions=profile.tensions,
        candidates=profile.candidates,
        self_model=profile.self_model,
        budget=profile.budget,
        used_redundancy_keys=profile.used_redundancy_keys,
        basis_refs=profile.basis_refs,
    )
    return profile, frame


def test_first_repair_pass_selects_bounded_evidence_acquisition() -> None:
    profile, frame = _frame(attempt=1)

    assert {issue.kind for issue in profile.issues} == {EpistemicIssueKind.ACQUISITION_GAP}
    assert profile.self_model.can_attempt("reason.generate")
    assert frame.intent.kind is MetaControlIntentKind.ACQUIRE_EVIDENCE
    assert frame.intent.action is not None
    assert frame.intent.action.capability == "reason.generate"
    assert frame.intent.action.effect_class == "read-only"


def test_reality_failure_changes_next_pass_to_representation_revision() -> None:
    profile, frame = _frame(
        attempt=2,
        retry_context="[verification] status=succeeded; active=true",
    )

    assert EpistemicIssueKind.CANDIDATE_SPACE_SUSPECTED_INCOMPLETE in {
        issue.kind for issue in profile.issues
    }
    assert StructuralTensionKind.REPEATED_REOPEN in {
        tension.kind for tension in profile.tensions
    }
    assert StructuralTensionKind.PERSISTENT_RESIDUAL in {
        tension.kind for tension in profile.tensions
    }
    assert frame.intent.kind is MetaControlIntentKind.REVISE_REPRESENTATION
    directive = render_meta_control_directive(frame)
    assert "META_INTENT=REVISE_REPRESENTATION" in directive
    assert "authorizes no effect" in directive


def test_line_ending_incident_is_representation_mismatch_not_semantic_failure() -> None:
    profile, frame = _frame(attempt=1, line_endings=True)

    assert EpistemicIssueKind.REPRESENTATION_MISMATCH in {
        issue.kind for issue in profile.issues
    }
    assert StructuralTensionKind.REPRESENTATION_INSTABILITY in {
        tension.kind for tension in profile.tensions
    }
    statements = "\n".join(candidate.statement for candidate in profile.candidates)
    assert "line-ending noise" in statements
    assert "semantic content change" in statements
    assert frame.intent.kind is MetaControlIntentKind.REVISE_REPRESENTATION


def test_self_model_capability_beliefs_do_not_encode_authority() -> None:
    profile, _frame_value = _frame(attempt=1)

    reason = profile.self_model.capability("reason.generate")
    shell = profile.self_model.capability("shell.exec")
    assert reason is not None
    assert shell is not None
    assert "effect authorization" in reason.cannot_establish
    assert "target recovery" in shell.cannot_establish
