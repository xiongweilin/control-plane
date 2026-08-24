from __future__ import annotations

from collections.abc import Iterable
from enum import StrEnum


class RestorationStatus(StrEnum):
    """Current judgment about whether the target reality has been restored.

    UNVERIFIED means no qualifying restoration evidence has yet been established;
    it makes no claim about reality. UNKNOWN means the current reality result cannot
    be determined, including when available evidence is unavailable or insufficient.
    FAILED and VERIFIED are evidence-bearing judgments that the target restoration
    condition is respectively unmet or met.
    """

    UNVERIFIED = "unverified"
    VERIFIED = "verified"
    FAILED = "failed"
    UNKNOWN = "unknown"


class ResolutionKind(StrEnum):
    """Disposition of a repair case, separate from lifecycle and reality judgment."""

    UNRESOLVED = "unresolved"
    RESTORED = "restored"
    NO_ACTION_REQUIRED = "no_action_required"
    REJECTED = "rejected"
    ROLLED_BACK = "rolled_back"
    SUPERSEDED = "superseded"
    ESCALATED = "escalated"


def _normalize_refs(values: Iterable[str], *, label: str) -> tuple[str, ...]:
    refs: list[str] = []
    seen: set[str] = set()
    for raw_ref in values:
        ref = raw_ref.strip()
        if not ref:
            raise ValueError(f"{label} refs must be non-empty strings")
        if ref not in seen:
            seen.add(ref)
            refs.append(ref)
    return tuple(refs)


def normalize_repair_resolution(
    resolution_kind: ResolutionKind | str,
    restoration_status: RestorationStatus | str,
    proof_refs: Iterable[str],
    basis_refs: Iterable[str] = (),
) -> tuple[
    ResolutionKind,
    RestorationStatus,
    tuple[str, ...],
    tuple[str, ...],
]:
    """Validate orthogonal disposition/restoration lineage invariants.

    ``basis_refs`` explain why the case disposition was selected. ``proof_refs``
    explain the current restoration judgment. Neither lineage grants closure
    authority by itself; product ClosureAuthority owns that question.
    """

    kind = ResolutionKind(resolution_kind)
    restoration = RestorationStatus(restoration_status)
    proofs = _normalize_refs(proof_refs, label="restoration proof")
    bases = _normalize_refs(basis_refs, label="resolution basis")

    if kind is not ResolutionKind.UNRESOLVED and not bases:
        raise ValueError(f"resolution_kind={kind.value} requires resolution basis refs")

    if restoration in {RestorationStatus.VERIFIED, RestorationStatus.FAILED} and not proofs:
        raise ValueError(
            f"restoration_status={restoration.value} requires restoration proof refs"
        )

    if (
        kind in {ResolutionKind.RESTORED, ResolutionKind.NO_ACTION_REQUIRED}
        and restoration is not RestorationStatus.VERIFIED
    ):
        raise ValueError(f"{kind.value} requires restoration_status=verified")

    return kind, restoration, proofs, bases
