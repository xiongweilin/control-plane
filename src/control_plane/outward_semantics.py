from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from .repair_resolution import ResolutionKind, RestorationStatus
from .state_machine import RepairState


def _text(row: Mapping[str, Any], key: str, default: str) -> str:
    value = row.get(key, default)
    text = str(value or "").strip()
    return text or default


def decode_ref_list(row: Mapping[str, Any], key: str) -> list[str]:
    """Read a stored lineage list without treating malformed data as evidence."""

    raw = row.get(key, "[]")
    try:
        value = json.loads(str(raw or "[]"))
    except (json.JSONDecodeError, TypeError, ValueError):
        return []
    if not isinstance(value, list):
        return []
    refs: list[str] = []
    seen: set[str] = set()
    for item in value:
        ref = str(item).strip()
        if ref and ref not in seen:
            seen.add(ref)
            refs.append(ref)
    return refs


def semantic_summary(*, status: str, resolution_kind: str, restoration_status: str) -> str:
    """Render existing facts without creating a new disposition or reality judgment."""

    triple = (status, resolution_kind, restoration_status)
    if triple == (
        RepairState.CLOSED.value,
        ResolutionKind.RESTORED.value,
        RestorationStatus.VERIFIED.value,
    ):
        return "已验证恢复"
    if triple == (
        RepairState.CLOSED.value,
        ResolutionKind.NO_ACTION_REQUIRED.value,
        RestorationStatus.VERIFIED.value,
    ):
        return "无需动作，已验证恢复"
    if triple == (
        RepairState.CLOSED.value,
        ResolutionKind.REJECTED.value,
        RestorationStatus.UNVERIFIED.value,
    ):
        return "方案被拒绝，处理结束；恢复未验证"
    if triple == (
        RepairState.ROLLED_BACK.value,
        ResolutionKind.ROLLED_BACK.value,
        RestorationStatus.UNVERIFIED.value,
    ):
        return "已执行回滚；目标恢复尚未验证"
    if triple == (
        RepairState.RECOVERING.value,
        ResolutionKind.UNRESOLVED.value,
        RestorationStatus.UNKNOWN.value,
    ):
        return "现实结果仍未知"
    return (
        f"工作流={status}；处置={resolution_kind}；"
        f"恢复判断={restoration_status}"
    )


def project_repair(row: Mapping[str, Any]) -> dict[str, Any]:
    """Project the three existing repair axes for API/notification consumers."""

    status = _text(row, "status", "unknown")
    resolution_kind = _text(row, "resolution_kind", ResolutionKind.UNRESOLVED.value)
    restoration_status = _text(
        row, "restoration_status", RestorationStatus.UNVERIFIED.value
    )
    return {
        "status": status,
        "resolution_kind": resolution_kind,
        "restoration_status": restoration_status,
        "semantic_summary": semantic_summary(
            status=status,
            resolution_kind=resolution_kind,
            restoration_status=restoration_status,
        ),
        "restoration_proof_refs": decode_ref_list(row, "restoration_proof_refs_json"),
        "resolution_basis_refs": decode_ref_list(row, "resolution_basis_refs_json"),
    }


def integrity_violation_flags(row: Mapping[str, Any]) -> dict[str, bool]:
    """Return observational integrity flags; this function performs no repair or mutation."""

    projection = project_repair(row)
    status = projection["status"]
    kind = projection["resolution_kind"]
    restoration = projection["restoration_status"]
    proof_refs = projection["restoration_proof_refs"]
    basis_refs = projection["resolution_basis_refs"]
    return {
        "closed_restored_unverified": (
            status == RepairState.CLOSED.value
            and kind == ResolutionKind.RESTORED.value
            and restoration != RestorationStatus.VERIFIED.value
        ),
        "restored_without_proof": (
            kind == ResolutionKind.RESTORED.value and not proof_refs
        ),
        "non_unresolved_without_basis": (
            kind != ResolutionKind.UNRESOLVED.value and not basis_refs
        ),
    }
