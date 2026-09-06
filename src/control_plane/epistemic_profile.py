from __future__ import annotations

from dataclasses import dataclass

from meta_controller import (
    Candidate,
    CandidateKind,
    CapabilityAvailability,
    CapabilityBelief,
    EpistemicIssue,
    EpistemicIssueKind,
    MetaControlFrame,
    MetaControlIntentKind,
    SearchBudget,
    StructuralTension,
    StructuralTensionKind,
    WorkingSelfModel,
)


@dataclass(frozen=True, slots=True)
class RepairEpistemicProfile:
    """Personal repair projection into meta-controller policy state.

    These objects are policy evidence and search candidates only. They do not
    establish truth, Work, execution authority, or effect authorization.
    """

    issues: tuple[EpistemicIssue, ...]
    tensions: tuple[StructuralTension, ...]
    candidates: tuple[Candidate, ...]
    self_model: WorkingSelfModel
    budget: SearchBudget
    used_redundancy_keys: frozenset[str]
    basis_refs: tuple[str, ...]


def build_repair_epistemic_profile(
    *,
    controller_ref: str,
    state_version: int,
    attempt: int,
    attempt_limit: int,
    is_line_ending_cleanup: bool,
    has_repo: bool,
    has_project: bool,
    has_maintenance_capability: bool,
    retry_context: str,
) -> RepairEpistemicProfile:
    if attempt < 1 or attempt_limit < 1:
        raise ValueError("repair attempt values must be positive")
    if state_version < 0:
        raise ValueError("state_version cannot be negative")

    basis = (f"controller:{controller_ref}:v{state_version}",)
    issues: list[EpistemicIssue] = []
    tensions: list[StructuralTension] = []
    candidates: list[Candidate] = []

    if attempt == 1:
        issues.append(
            EpistemicIssue(
                kind=EpistemicIssueKind.ACQUISITION_GAP,
                statement=(
                    "The incident has not yet been localized by a current discriminating "
                    "diagnosis."
                ),
                scope=controller_ref,
                basis_refs=basis,
                decision_relevance=0.95,
            )
        )
    else:
        issues.append(
            EpistemicIssue(
                kind=EpistemicIssueKind.CANDIDATE_SPACE_SUSPECTED_INCOMPLETE,
                statement=(
                    "Reality contradicted the previous repair closure; repeating the same "
                    "root-cause partition is not sufficient."
                ),
                scope=controller_ref,
                basis_refs=basis,
                decision_relevance=1.0,
            )
        )
        tensions.append(
            StructuralTension(
                kind=StructuralTensionKind.REPEATED_REOPEN,
                statement=(
                    "A prior bounded repair did not close the incident in reality, so the "
                    "next pass must introduce a new distinction."
                ),
                basis_refs=basis,
                persistence=max(1, attempt - 1),
                recurrence=max(1, attempt - 1),
                decision_relevance=1.0,
            )
        )
        if retry_context.strip():
            tensions.append(
                StructuralTension(
                    kind=StructuralTensionKind.PERSISTENT_RESIDUAL,
                    statement="Prior Work/verification evidence contains an unresolved residual.",
                    basis_refs=basis,
                    persistence=max(1, attempt - 1),
                    recurrence=max(1, attempt - 1),
                    decision_relevance=0.9,
                )
            )
        candidates.append(
            Candidate(
                kind=CandidateKind.REPRESENTATION,
                statement=(
                    "Repartition the live root-cause hypotheses before proposing another "
                    "repair; identify which assumption in the previous closure failed."
                ),
                scope=controller_ref,
                basis_refs=basis,
                expected_observable_difference=(
                    "The revised partition must imply at least one observation that the previous "
                    "diagnosis did not use."
                ),
                expected_discrimination=0.92,
                decision_relevance=1.0,
                estimated_cost=0.5,
                estimated_latency=1.0,
                redundancy_key=f"representation-revision:{attempt}",
            )
        )

    if is_line_ending_cleanup:
        issues.append(
            EpistemicIssue(
                kind=EpistemicIssueKind.REPRESENTATION_MISMATCH,
                statement=(
                    "A surface dirty-state distinction may collapse semantic change and "
                    "line-ending representation noise."
                ),
                scope=controller_ref,
                basis_refs=basis,
                decision_relevance=1.0,
            )
        )
        tensions.append(
            StructuralTension(
                kind=StructuralTensionKind.REPRESENTATION_INSTABILITY,
                statement=(
                    "Repository dirtiness is unstable under the deterministic line-ending "
                    "equivalence relation."
                ),
                basis_refs=basis,
                decision_relevance=1.0,
            )
        )
        candidates.append(
            Candidate(
                kind=CandidateKind.REPRESENTATION,
                statement=(
                    "Separate semantic content change from line-ending noise using the exact "
                    "normalization equivalence before classifying the repository state."
                ),
                scope=controller_ref,
                basis_refs=basis,
                expected_observable_difference=(
                    "Normalization equivalence distinguishes semantic edits from representational "
                    "noise without broadening scope."
                ),
                expected_discrimination=0.98,
                decision_relevance=1.0,
                estimated_cost=0.1,
                estimated_latency=0.1,
                redundancy_key="line-ending-representation-equivalence",
            )
        )

    candidates.append(
        Candidate(
            kind=CandidateKind.ACQUISITION,
            statement=(
                "Acquire one bounded current diagnosis that distinguishes the live root-cause "
                "hypotheses before selecting a repair."
            ),
            scope=controller_ref,
            basis_refs=basis,
            discriminates_between=("current-root-cause-hypotheses",),
            expected_observable_difference=(
                "The diagnosis must identify a concrete reality difference that changes the "
                "repair choice."
            ),
            required_capabilities=("reason.generate",),
            expected_discrimination=0.85 if attempt == 1 else 0.55,
            decision_relevance=1.0,
            estimated_cost=1.0,
            estimated_latency=1.0,
            redundancy_key=f"bounded-diagnosis:{attempt}",
        )
    )

    capabilities = [
        CapabilityBelief(
            capability_ref="reason.generate",
            availability=CapabilityAvailability.AVAILABLE,
            can_observe=("bounded diagnosis output",),
            cannot_establish=("target recovery", "effect authorization"),
            effect_class="read-only",
            expected_cost=1.0,
            expected_latency=1.0,
            basis_refs=basis,
        ),
        CapabilityBelief(
            capability_ref="monitor.alert.active",
            availability=CapabilityAvailability.AVAILABLE,
            can_observe=("triggering alert active state",),
            cannot_establish=("root cause",),
            effect_class="read-only",
            expected_cost=0.2,
            expected_latency=0.2,
            basis_refs=basis,
        ),
    ]
    if has_repo:
        capabilities.append(
            CapabilityBelief(
                capability_ref="shell.exec",
                availability=CapabilityAvailability.AVAILABLE,
                can_observe=("bounded repository-local execution outcome",),
                cannot_establish=("target recovery",),
                effect_class="internal-reversible",
                basis_refs=basis,
            )
        )
    if has_project:
        capabilities.append(
            CapabilityBelief(
                capability_ref="docker.compose.up",
                availability=CapabilityAvailability.AVAILABLE,
                can_observe=("bounded project apply result",),
                cannot_establish=("alert recovery",),
                effect_class="external-effect",
                basis_refs=basis,
            )
        )
    if has_maintenance_capability:
        capabilities.append(
            CapabilityBelief(
                capability_ref="maintenance.profile-action",
                availability=CapabilityAvailability.AVAILABLE,
                can_observe=("bounded maintenance result",),
                cannot_establish=("alert recovery",),
                effect_class="internal-reversible",
                basis_refs=basis,
            )
        )

    self_model = WorkingSelfModel(
        capabilities=tuple(capabilities),
        resources=(
            ("remaining-autonomous-attempts", float(max(0, attempt_limit - attempt + 1))),
        ),
        known_blind_spots=(
            "provider success does not establish target recovery",
            "current capability belief does not grant execution authority",
        ),
        basis_refs=basis,
    )
    return RepairEpistemicProfile(
        issues=tuple(issues),
        tensions=tuple(tensions),
        candidates=tuple(candidates),
        self_model=self_model,
        budget=SearchBudget(
            max_actions=max(1, min(2, attempt_limit - attempt + 1)),
            max_cost=3.0,
            max_latency=30.0,
        ),
        used_redundancy_keys=frozenset(),
        basis_refs=basis,
    )


def render_meta_control_directive(frame: MetaControlFrame) -> str:
    """Render policy intent as bounded cognition guidance, never as authority."""

    intent = frame.intent
    if intent.kind is MetaControlIntentKind.REVISE_REPRESENTATION:
        return (
            "META_INTENT=REVISE_REPRESENTATION\n"
            "Do not repeat the previous root-cause partition. Change the working distinctions "
            "or equivalence relation first, then name one new observable difference that would "
            "separate the revised hypotheses. This directive authorizes no effect."
        )
    if intent.kind is MetaControlIntentKind.ACQUIRE_EVIDENCE:
        action = intent.action
        instruction = action.instruction if action is not None else "acquire bounded evidence"
        return (
            "META_INTENT=ACQUIRE_EVIDENCE\n"
            f"Use this cognition pass to {instruction}. Treat proxy/provider success as evidence "
            "only for its bounded proposition; it does not establish target recovery."
        )
    if intent.kind is MetaControlIntentKind.EFFECTFUL_EXPERIMENT:
        return (
            "META_INTENT=EFFECTFUL_EXPERIMENT\n"
            "Do not execute the experiment during diagnosis. Describe the discriminating effect "
            "candidate so it can pass through CognitiveClosure, WorkProposal and Kernel effect "
            "boundaries."
        )
    if intent.kind is MetaControlIntentKind.WAIT:
        return (
            "META_INTENT=WAIT\n"
            "No admissible discriminating action is available. Do not manufacture a closure; "
            "identify the missing evidence, capability or distinction explicitly."
        )
    return f"META_INTENT={intent.kind.value.upper().replace('-', '_')}"
