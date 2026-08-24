from __future__ import annotations

from collections.abc import Iterable
from enum import StrEnum


class RestorationStatus(StrEnum):
    """Current judgment about whether the target reality has been restored.

    UNVERIFIED means no qualifying restoration evidence has yet been established;
    it makes no claim about reality. UNKNOWN means observation or checking occurred,
    but the result remains indeterminate. FAILED and VERIFIED are evidence-bearing
    judgments that the target restoration condition is respectively unmet or met.
    """

    UNVERIFIED = "unverified"
    VERIFIED = "verified"
    FAILED = "failed"
    UNKNOWN = "unknown"


class ResolutionKind(StrEnum):
    """Disposition of the repair case, orthogonal to workflow lifecycle and reality judgment."""

    UNRESOLVED = "unresolved"
    RESTORED = "restored"
    NO_ACTION_REQUIRED = "no_action_required"
    REJECTED = "rejected"
    ROLLED_BACK = "rolled_back"
    SUPERSEDED = "superseded"
    ESCALATED = "escalated"


def normalize_repair_resolution(
    resolution_kind: ResolutionKind | str,
    restoration_status: RestorationStatus | str,
    proof_refs: Iterable[str],
) -> tuple[ResolutionKind, RestorationStatus, tuple[str, ...]]:
    """Validate only structural C2 invariants; this function grants no closure authority."""

    kind = ResolutionKind(resolution_kind)
    restoration = RestorationStatus(restoration_status)
    refs: list[str] = []
    seen: set[str] = set()
    for raw_ref in proof_refs:
        ref = raw_ref.strip()
        if not ref:
  raise ValueError("restoration proof refs must be non-empty strings")
        if ref not in seen:
  seen.add(ref)
  refs.append(ref)

    if kind in {ResolutionKind.RESTORED, ResolutionKind.NO_ACTION_REQUIRED}:
        if restoration is not RestorationStatus.VERIFIED:
  raise ValueError(f"{kind.value} requires restoration_status=verified")
        if not refs:
  raise ValueError(f"{kind.value} requires at least one restoration proof ref")

    return kind, restoration, tuple(refs)
