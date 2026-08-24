from __future__ import annotations

import json
import time
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from .repair_resolution import ResolutionKind, RestorationStatus
from .state_machine import RepairState, StateMachineError, require_transition


class ClosureAuthorityError(RuntimeError):
    """Raised when legacy case closure is not supported by canonical evidence."""


@dataclass(frozen=True, slots=True)
class RollbackExecutionReceipt:
    """Typed execution fact consumed by rollback disposition.

    This is not restoration proof. It records only which authorized rollback
    operation ran and the Runtime result returned for that request.
    """

    capability: str
    request_id: str
    provider_id: str
    status: str


class ClosureAuthority:
    """Own legacy case-terminal disposition projection from durable facts.

    Portable CompletionAuthority remains the authority for objective/verification
    terminal success. This class never re-runs a verifier and never accepts caller-
    supplied restoration truth or proof refs for positive closure.
    """

    def __init__(self, legacy_store: Any, portable_store: Any) -> None:
        self.legacy_store = legacy_store
        self.portable_store = portable_store

    @staticmethod
    def _normalized_refs(raw: object) -> list[str]:
        if not isinstance(raw, list):
            return []
        refs: list[str] = []
        seen: set[str] = set()
        for item in raw:
            ref = str(item).strip()
            if ref and ref not in seen:
                seen.add(ref)
                refs.append(ref)
        return refs

    @staticmethod
    def _row_refs(row: Any, field: str) -> list[str]:
        try:
            raw = json.loads(str(row[field] or "[]"))
        except (json.JSONDecodeError, TypeError, ValueError):
            raise ClosureAuthorityError(f"repair {field} is not valid JSON") from None
        if not isinstance(raw, list):
            raise ClosureAuthorityError(f"repair {field} must be a JSON list")
        refs = [str(item).strip() for item in raw if str(item).strip()]
        if len(refs) != len(set(refs)):
            raise ClosureAuthorityError(f"repair {field} contains duplicate refs")
        return refs

    def _repair_row(self, repair_id: str) -> Any:
        row = self.legacy_store.get_repair(repair_id)
        if row is None:
            raise ClosureAuthorityError(f"unknown repair: {repair_id}")
        return row

    def _canonical_pair(self, repair_id: str) -> tuple[Any, Any]:
        get_work = getattr(self.portable_store, "get_work", None)
        get_run = getattr(self.portable_store, "get_run", None)
        if not callable(get_work) or not callable(get_run):
            raise ClosureAuthorityError("portable store does not expose canonical Work/Run reads")
        work = get_work(f"work_legacy_{repair_id}")
        run = get_run(f"run_legacy_{repair_id}")
        if work is None or run is None:
            raise ClosureAuthorityError("canonical Work/Run pair is missing")
        if getattr(run, "work_id", None) != getattr(work, "id", None):
            raise ClosureAuthorityError("canonical Work/Run binding mismatch")
        return work, run

    def _terminal_restoration_bundle(self, repair_id: str) -> tuple[Any, Any, list[str]]:
        work, run = self._canonical_pair(repair_id)
        if getattr(work, "status", None) != "completed" or getattr(run, "status", None) != "succeeded":
            raise ClosureAuthorityError("canonical Work/Run is not a successful terminal pair")

        work_meta = getattr(work, "metadata", {})
        run_meta = getattr(run, "metadata", {})
        if not isinstance(work_meta, dict) or not isinstance(run_meta, dict):
            raise ClosureAuthorityError("canonical terminal metadata is missing")

        work_refs = self._normalized_refs(work_meta.get("_completion_proof_refs"))
        run_refs = self._normalized_refs(run_meta.get("_completion_proof_refs"))
        if not work_refs or work_refs != run_refs:
            raise ClosureAuthorityError("canonical terminal completion proof refs are missing or asymmetric")

        audit_fields = (
            "completion_required_obligations",
            "completion_covered_obligations",
            "completion_missing_obligations",
        )
        audit: dict[str, list[str]] = {}
        for field in audit_fields:
            work_value = self._normalized_refs(work_meta.get(field))
            run_value = self._normalized_refs(run_meta.get(field))
            if work_value != run_value:
                raise ClosureAuthorityError(f"canonical terminal {field} is asymmetric")
            audit[field] = work_value

        if audit["completion_missing_obligations"]:
            raise ClosureAuthorityError("canonical terminal pair still has missing obligations")
        required = set(audit["completion_required_obligations"])
        covered = set(audit["completion_covered_obligations"])
        if not required.issubset(covered):
            raise ClosureAuthorityError("canonical terminal obligation coverage is incomplete")
        return work, run, work_refs

    def _human_decision(self, repair_id: str, selected_option: str) -> Any:
        work, run = self._canonical_pair(repair_id)
        work_meta = getattr(work, "metadata", {})
        run_meta = getattr(run, "metadata", {})
        if not isinstance(work_meta, dict) or not isinstance(run_meta, dict):
            raise ClosureAuthorityError("canonical human-approval metadata is missing")

        work_ref = str(work_meta.get("human_approval_decision_ref", "")).strip()
        run_ref = str(run_meta.get("human_approval_decision_ref", "")).strip()
        if not work_ref or work_ref != run_ref:
            raise ClosureAuthorityError("Work/Run human Decision refs are missing or asymmetric")
        if (
            str(work_meta.get("human_approval_action", "")).strip() != selected_option
            or str(run_meta.get("human_approval_action", "")).strip() != selected_option
        ):
            raise ClosureAuthorityError("Work/Run human approval action contradicts disposition")

        get_decision = getattr(self.portable_store, "get_decision", None)
        if not callable(get_decision):
            raise ClosureAuthorityError("portable store does not expose Decision reads")
        decision = get_decision(work_ref)
        if decision is None:
            raise ClosureAuthorityError("durable human Decision is missing")
        if getattr(decision, "decision_type", None) != "human-approval":
            raise ClosureAuthorityError("case disposition requires a human-approval Decision")
        if getattr(decision, "selected_option", None) != selected_option:
            raise ClosureAuthorityError(
                f"human Decision does not authorize disposition {selected_option!r}"
            )
        if not list(getattr(decision, "authorized_by", []) or []):
            raise ClosureAuthorityError("human Decision has no authorized principal")
        if getattr(decision, "work_id", None) != getattr(work, "id", None):
            raise ClosureAuthorityError("human Decision is not bound to the current repair Work")
        decision_meta = getattr(decision, "metadata", {})
        if not isinstance(decision_meta, dict) or decision_meta.get("repair_id") != repair_id:
            raise ClosureAuthorityError("human Decision is not bound to the current repair")
        return decision

    def _set_resolution_preserving_restoration(
        self,
        repair_id: str,
        *,
        resolution_kind: ResolutionKind,
        basis_refs: Iterable[str],
    ) -> None:
        row = self._repair_row(repair_id)
        try:
            restoration = RestorationStatus(str(row["restoration_status"]))
        except ValueError as exc:
            raise ClosureAuthorityError("repair restoration_status is invalid") from exc
        proof_refs = self._row_refs(row, "restoration_proof_refs_json")
        self.legacy_store.set_repair_resolution(
            repair_id,
            resolution_kind=resolution_kind,
            restoration_status=restoration,
            proof_refs=proof_refs,
            basis_refs=tuple(basis_refs),
        )

    def _close_legacy(self, repair_id: str, *, result: str) -> None:
        row = self._repair_row(repair_id)
        current = RepairState(str(row["status"]))
        if current is RepairState.CLOSED:
            return
        try:
            require_transition(current, RepairState.CLOSED)
        except StateMachineError:
            try:
                require_transition(current, RepairState.VERIFIED)
            except StateMachineError as exc:
                raise ClosureAuthorityError(
                    f"canonical evidence cannot close legacy state {current.value!r}"
                ) from exc
            self.legacy_store.set_repair_status(repair_id, RepairState.VERIFIED.value)
            current = RepairState.VERIFIED
            require_transition(current, RepairState.CLOSED)
        self.legacy_store.set_repair_status(
            repair_id,
            RepairState.CLOSED.value,
            finished_at=int(time.time()),
            result=result,
        )

    def close_restored(self, repair_id: str) -> None:
        """Project an already-accepted canonical terminal success into legacy closure."""

        work, run, proof_refs = self._terminal_restoration_bundle(repair_id)
        expected_basis = [work.id, run.id]
        row = self._repair_row(repair_id)
        if row["status"] == RepairState.CLOSED.value:
            existing_proofs = self._row_refs(row, "restoration_proof_refs_json")
            existing_basis = self._row_refs(row, "resolution_basis_refs_json")
            if (
                row["resolution_kind"] == ResolutionKind.RESTORED.value
                and row["restoration_status"] == RestorationStatus.VERIFIED.value
                and existing_proofs == proof_refs
                and existing_basis == expected_basis
            ):
                return
            raise ClosureAuthorityError("closed repair contradicts canonical restored projection")

        if row["resolution_kind"] == ResolutionKind.RESTORED.value:
            existing_proofs = self._row_refs(row, "restoration_proof_refs_json")
            existing_basis = self._row_refs(row, "resolution_basis_refs_json")
            if (
                row["restoration_status"] != RestorationStatus.VERIFIED.value
                or existing_proofs != proof_refs
                or existing_basis != expected_basis
            ):
                raise ClosureAuthorityError("partial restored projection contradicts canonical terminal bundle")
        else:
            self.legacy_store.set_repair_resolution(
                repair_id,
                resolution_kind=ResolutionKind.RESTORED,
                restoration_status=RestorationStatus.VERIFIED,
                proof_refs=proof_refs,
                basis_refs=tuple(expected_basis),
            )
        metadata = getattr(work, "metadata", {})
        summary = str(metadata.get("verification_summary", "") if isinstance(metadata, dict) else "")
        self._close_legacy(repair_id, result=summary or "restoration verified")

    def close_rejected(self, repair_id: str) -> None:
        """Close case disposition from a durable human reject Decision only."""

        decision = self._human_decision(repair_id, "reject")
        row = self._repair_row(repair_id)
        expected_basis = [decision.id]
        if row["status"] == RepairState.CLOSED.value:
            basis = self._row_refs(row, "resolution_basis_refs_json")
            if row["resolution_kind"] == ResolutionKind.REJECTED.value and basis == expected_basis:
                return
            raise ClosureAuthorityError("closed repair contradicts durable rejection Decision")
        self._set_resolution_preserving_restoration(
            repair_id,
            resolution_kind=ResolutionKind.REJECTED,
            basis_refs=expected_basis,
        )
        self._close_legacy(repair_id, result="rejected")

    def record_rolled_back(
        self,
        repair_id: str,
        executions: Iterable[RollbackExecutionReceipt],
    ) -> None:
        """Record rollback disposition only after authorized effects succeeded.

        Human authorization proves the choice to roll back. Runtime execution
        receipts prove only that the required rollback operations returned
        ``succeeded``. Neither fact is promoted to restoration verification.
        """

        decision = self._human_decision(repair_id, "rollback")
        receipts = list(executions)
        if not receipts:
            raise ClosureAuthorityError("rollback disposition requires execution receipts")
        request_ids: list[str] = []
        for receipt in receipts:
            if receipt.capability not in {"git.rollback", "docker.restart"}:
                raise ClosureAuthorityError("rollback receipt has non-rollback capability")
            if receipt.status != "succeeded":
                raise ClosureAuthorityError("rollback disposition requires every execution to succeed")
            request_id = receipt.request_id.strip()
            provider_id = receipt.provider_id.strip()
            if not request_id or not provider_id:
                raise ClosureAuthorityError("rollback execution receipt is missing Runtime provenance")
            if request_id in request_ids:
                raise ClosureAuthorityError("rollback execution request ids must be unique")
            request_ids.append(request_id)

        expected_basis = [decision.id, *request_ids]
        row = self._repair_row(repair_id)
        if row["status"] == RepairState.ROLLED_BACK.value:
            basis = self._row_refs(row, "resolution_basis_refs_json")
            if row["resolution_kind"] == ResolutionKind.ROLLED_BACK.value and basis == expected_basis:
                return
            raise ClosureAuthorityError("rolled-back repair contradicts rollback execution basis")
        self._set_resolution_preserving_restoration(
            repair_id,
            resolution_kind=ResolutionKind.ROLLED_BACK,
            basis_refs=expected_basis,
        )
        current = RepairState(str(row["status"]))
        try:
            require_transition(current, RepairState.ROLLED_BACK)
        except StateMachineError as exc:
            raise ClosureAuthorityError(
                f"rollback disposition cannot project from legacy state {current.value!r}"
            ) from exc
        self.legacy_store.set_repair_status(
            repair_id,
            RepairState.ROLLED_BACK.value,
            finished_at=int(time.time()),
        )

    def reconcile_restored_projection(self, repair_id: str) -> bool:
        """Repair only a current crash-window under-projection.

        Historical ``CLOSED + UNRESOLVED`` rows are deliberately not upgraded:
        C2 migrated those rows to unknown/unresolved because retrospective
        evidence was not established at migration time.
        """

        row = self._repair_row(repair_id)
        status = str(row["status"])
        resolution = str(row["resolution_kind"])
        restoration = str(row["restoration_status"])

        if status == RepairState.CLOSED.value and resolution == ResolutionKind.UNRESOLVED.value:
            return False

        if status == RepairState.CLOSED.value:
            if (
                resolution == ResolutionKind.RESTORED.value
                and restoration == RestorationStatus.VERIFIED.value
            ):
                work, run, proofs = self._terminal_restoration_bundle(repair_id)
                if self._row_refs(row, "restoration_proof_refs_json") != proofs:
                    raise ClosureAuthorityError("closed restored projection has stale proof lineage")
                if self._row_refs(row, "resolution_basis_refs_json") != [work.id, run.id]:
                    raise ClosureAuthorityError("closed restored projection has stale disposition basis")
            return False

        if resolution not in {ResolutionKind.UNRESOLVED.value, ResolutionKind.RESTORED.value}:
            return False
        if resolution == ResolutionKind.RESTORED.value and restoration != RestorationStatus.VERIFIED.value:
            raise ClosureAuthorityError("partial restored projection lacks verified restoration status")

        try:
            work, run = self._canonical_pair(repair_id)
        except ClosureAuthorityError:
            return False
        if getattr(work, "status", None) != "completed" or getattr(run, "status", None) != "succeeded":
            return False
        self.close_restored(repair_id)
        return True
