"""Deep adapter from the legacy repair API to the portable runtime authority.

The adapter owns the migration seam.  Callers provide only a repair id, a
repository and a prompt; this module materialises the canonical Work/Run,
binds the request to the current git version, records the personal-owner
grant, and invokes the portable ``RealityBoundary``.  The legacy SQLite row is
read only as migration input and remains a compatibility projection.
"""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from portable_runtime.core.capabilities import CapabilityRequest, CapabilityResult
from portable_runtime.core.models import Artifact, Checkpoint, Decision, Run, Work, utcnow
from portable_runtime.core.runtime import Runtime
from portable_runtime.records.authorization import AuthorizationGrant
from portable_runtime.records.models import BaseRecord
from portable_runtime.records.relations import RecordRelation
from portable_runtime.stores.migration import dual_write_repair

VersionResolver = Callable[[str], Awaitable[str]]


class PortableRuntimeAuthority:
    """Canonical execution seam for personal-agent work.

    The authority is deliberately small: ``invoke`` is the only execution
    method.  All durable governance facts are written before it calls
    ``Runtime.capabilities``.  The portable store owns these Work/Run,
    Decision, AuthorizationGrant and qualification records; the legacy store
    is never consulted by ``RealityBoundary``.
    """

    def __init__(
        self,
        runtime: Runtime,
        *,
        legacy_store: Any | None = None,
        version_resolver: VersionResolver | None = None,
        actor_ref: str = "personal-agent",
        # Principal references participate in the portable semantic graph;
        # keep the default namespaced so graph validation treats it as an
        # external human principal rather than a dangling local id.
        principal_ref: str = "human:personal-owner",
    ) -> None:
        self.runtime = runtime
        self.legacy_store = legacy_store
        self.version_resolver = version_resolver
        self.actor_ref = actor_ref
        self.principal_ref = principal_ref

    @property
    def capability_service(self) -> Any:
        """Expose the runtime's full CapabilityService for inspection/tests."""

        return self.runtime.capabilities

    @staticmethod
    def _stable_token(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]

    def _legacy_row(self, repair_id: str) -> Mapping[str, object]:
        if self.legacy_store is not None:
            getter = getattr(self.legacy_store, "get_repair", None)
            if callable(getter):
                row = getter(repair_id)
                if row is not None:
                    return dict(row)
        return {
            "id": repair_id,
            "fingerprint": f"migration:{repair_id}",
            "payload_json": "",
            "status": "open",
        }

    async def _version_ref(self, repo: str) -> str:
        if self.version_resolver is None:
            raise RuntimeError("portable authority requires a git version resolver")
        raw = (await self.version_resolver(repo)).strip()
        if not raw:
            raise RuntimeError(f"portable authority could not resolve git version for {repo}")
        return f"git:{raw.splitlines()[0].strip()}"

    def _record(self, record: BaseRecord) -> BaseRecord:
        getter = getattr(self.runtime.store, "get_record", None)
        if callable(getter) and getter(record.id) is not None:
            existing = getter(record.id)
            return existing
        self.runtime.store.save_record(record)
        return record

    def _persist_execution_outcome(
        self,
        work: Work,
        run: Run,
        result: CapabilityResult,
    ) -> None:
        """Project provider execution without confusing it with verification.

        ``unknown`` means that the provider cannot currently prove the
        external outcome.  It is therefore deliberately kept recoverable:
        canonical Work/Run remain waiting and carry enough metadata for a
        later reconciliation attempt.  Only an explicit provider failure is
        projected as an execution failure; this method never writes a
        verification failure record.
        """

        now = utcnow()
        outcome = str(result.status)
        work_metadata = dict(work.metadata)
        run_metadata = dict(run.metadata)
        outcome_metadata = {
            "execution_outcome": outcome,
            "execution_request_ref": result.request_id,
            "execution_provider_ref": result.provider_id,
            "execution_message": (result.message or "")[:4_000],
        }
        work_metadata.update(outcome_metadata)
        run_metadata.update(outcome_metadata)
        if outcome == "unknown":
            recovery_metadata = {
                "reconciliation_required": True,
                "reconciliation_status": "required",
                "reconciliation_reason": (result.message or "provider outcome is unknown")[:4_000],
            }
            work_metadata.update(recovery_metadata)
            run_metadata.update(recovery_metadata)
            self.runtime.store.save_work(
                work.model_copy(update={"status": "waiting", "metadata": work_metadata, "updated_at": now})
            )
            self.runtime.store.save_run(
                run.model_copy(update={"status": "waiting", "metadata": run_metadata})
            )
            return

        if outcome == "succeeded":
            # A provider success still waits for the deterministic verifier.
            work_status = "waiting"
            run_status = "waiting"
            run_update: dict[str, Any] = {"status": run_status, "metadata": run_metadata}
        else:
            work_status = "failed"
            run_status = "failed"
            run_update = {
                "status": run_status,
                "metadata": run_metadata,
                "ended_at": now,
            }
        self.runtime.store.save_work(
            work.model_copy(update={"status": work_status, "metadata": work_metadata, "updated_at": now})
        )
        self.runtime.store.save_run(run.model_copy(update=run_update))

    def mark_reconciliation_required(self, repair_id: str, summary: str) -> None:
        """Keep canonical execution open when reality cannot be observed."""

        work = self.runtime.store.get_work(f"work_legacy_{repair_id}")
        run = self.runtime.store.get_run(f"run_legacy_{repair_id}")
        if work is None or run is None:
            return
        now = utcnow()
        message = summary[:4_000]
        work_metadata = dict(work.metadata)
        run_metadata = dict(run.metadata)
        for metadata in (work_metadata, run_metadata):
            metadata.update(
                {
                    "execution_outcome": "unknown",
                    "reconciliation_required": True,
                    "reconciliation_status": "required",
                    "reconciliation_reason": message,
                }
            )
        self.runtime.store.save_work(
            work.model_copy(update={"status": "waiting", "metadata": work_metadata, "updated_at": now})
        )
        self.runtime.store.save_run(run.model_copy(update={"status": "waiting", "metadata": run_metadata})
        )

    def record_task_result_artifact(
        self,
        repair_id: str,
        *,
        path: Path,
        run_id: str,
        checksum: str,
    ) -> str | None:
        """Register a task transcript only when it is tied to its canonical run.

        A readable transcript is an execution artifact, not proof that an
        arbitrary natural-language task was substantively satisfied.  This
        helper therefore records only the independently observable artifact
        and leaves final verification to the task postcondition verifier.
        """

        work = self.runtime.store.get_work(f"work_legacy_{repair_id}")
        run = self.runtime.store.get_run(f"run_legacy_{repair_id}")
        if work is None or run is None or run.id != run_id or work.kind != "generic-task":
            return None
        try:
            resolved = path.resolve(strict=True)
            if not resolved.is_file() or not resolved.stat().st_size:
                return None
        except OSError:
            return None
        token = self._stable_token(f"task-artifact:{repair_id}:{resolved}:{checksum}")
        artifact_id = f"artifact_{repair_id}_{token}_task_result"
        artifact = Artifact(
            id=artifact_id,
            kind="agent-session",
            media_type="application/jsonl",
            uri=resolved.as_uri(),
            created_by_run_id=run.id,
            created_by_provider_id="codex-primary",
            checksum=checksum,
            metadata={
                "qualification_kind": "task-result-artifact",
                "repair_id": repair_id,
                "run_id": run.id,
                "source": "control-plane.task-result-verifier",
            },
        )
        existing = self.runtime.store.get_artifact(artifact_id)
        if existing is None:
            self.runtime.store.save_artifact(artifact)
        refs = list(work.artifact_refs)
        if artifact_id not in refs:
            refs.append(artifact_id)
        work_metadata = dict(work.metadata)
        run_metadata = dict(run.metadata)
        work_metadata.update({"task_result_artifact_ref": artifact_id, "task_result_artifact_checksum": checksum})
        run_metadata.update({"task_result_artifact_ref": artifact_id, "task_result_artifact_checksum": checksum})
        self.runtime.store.save_work(
            work.model_copy(
                update={"artifact_refs": refs, "metadata": work_metadata, "updated_at": utcnow()}
            )
        )
        self.runtime.store.save_run(run.model_copy(update={"metadata": run_metadata}))
        return artifact_id

    def mark_verification_required(self, repair_id: str, summary: str) -> None:
        """Keep a task waiting when execution completed without a verifier proof."""

        work = self.runtime.store.get_work(f"work_legacy_{repair_id}")
        run = self.runtime.store.get_run(f"run_legacy_{repair_id}")
        if work is None or run is None:
            return
        message = summary[:4_000]
        work_metadata = dict(work.metadata)
        run_metadata = dict(run.metadata)
        for metadata in (work_metadata, run_metadata):
            metadata.update(
                {
                    "verification_required": True,
                    "verification_status": "unavailable",
                    "verification_reason": message,
                }
            )
        self.runtime.store.save_work(
            work.model_copy(update={"status": "waiting", "metadata": work_metadata, "updated_at": utcnow()})
        )
        self.runtime.store.save_run(run.model_copy(update={"status": "waiting", "metadata": run_metadata}))

    def record_task_delivery_verification(
        self,
        repair_id: str,
        *,
        summary: str,
        evidence_refs: list[str] | None = None,
    ) -> list[str]:
        """Record delivery of a task result without asserting its objective.

        A readable, Run-bound transcript proves that the provider delivered a
        result artifact.  It does not prove that an arbitrary natural-language
        task objective was satisfied.  Keep that distinction durable: the
        canonical Work/Run remain ``waiting`` and only delivery-scoped fields
        are populated.  A future task-specific verifier may promote the Work
        to objective verification independently.
        """

        work = self.runtime.store.get_work(f"work_legacy_{repair_id}")
        run = self.runtime.store.get_run(f"run_legacy_{repair_id}")
        if work is None or run is None or work.kind != "generic-task":
            return []

        message = summary[:4_000]
        token = self._stable_token(f"task-delivery-verification:{repair_id}:{message}")
        version_refs = list(work.metadata.get("subject_version_refs", []))
        delivery = self._record(
            BaseRecord(
                id=f"record_{repair_id}_{token}_delivery_verification",
                record_type="Assertion",
                lifecycle_status="current",
                epistemic_status="supported",
                metadata={
                    "qualification_kind": "task-delivery-verification",
                    "result": "pass",
                    "verification_scope": "delivery",
                    "objective_status": "unverified",
                    "target_refs": [work.id, run.id],
                    "subject_version_refs": version_refs,
                    "verifier_ref": "control-plane.task-result-postcondition",
                    "evidence_refs": list(evidence_refs or []),
                    "summary": message,
                },
            )
        )
        evidence = self._record(
            BaseRecord(
                id=f"record_{repair_id}_{token}_delivery_evidence",
                record_type="EvidenceArtifact",
                lifecycle_status="current",
                metadata={
                    "qualification_kind": "task-delivery-evidence",
                    "verification_scope": "delivery",
                    "objective_status": "unverified",
                    "target_refs": [work.id, run.id],
                    "subject_version_refs": version_refs,
                    "verifier_ref": "control-plane.task-result-postcondition",
                    "evidence_refs": list(evidence_refs or []),
                    "summary": message,
                },
            )
        )
        relation_id = f"relation_{repair_id}_{token}_delivery_verification"
        get_relation = getattr(self.runtime.store, "get_relation", None)
        relation = get_relation(relation_id) if callable(get_relation) else None
        if not isinstance(relation, RecordRelation):
            relation = RecordRelation(
                id=relation_id,
                relation_type="validated-under",
                subject_ref=evidence.id,
                object_ref=delivery.id,
                scope={
                    "work_id": work.id,
                    "run_id": run.id,
                    "verification_scope": "delivery",
                    "objective_status": "unverified",
                },
                created_by="control-plane.task-result-postcondition",
                metadata={"qualification_kind": "task-delivery-verification"},
            )
            save_relation = getattr(self.runtime.store, "save_relation", None)
            if not callable(save_relation):
                raise RuntimeError("portable authority store cannot persist delivery verification relation")
            save_relation(relation)

        refs = [delivery.id, evidence.id, relation.id]
        common_metadata = {
            "delivery_verification_refs": refs,
            "delivery_verification_status": "passed",
            "delivery_verification_summary": message,
            "delivery_verified": True,
            "verification_scope": "delivery",
            "objective_status": "unverified",
            "objective_verification_status": "unavailable",
            "verification_required": True,
            "verification_status": "unavailable",
            "verification_reason": "task objective verifier is unavailable",
        }
        work_metadata = dict(work.metadata)
        work_metadata.update(common_metadata)
        run_metadata = dict(run.metadata)
        run_metadata.update(common_metadata)
        self.runtime.store.save_work(
            work.model_copy(update={"status": "waiting", "metadata": work_metadata, "updated_at": utcnow()})
        )
        self.runtime.store.save_run(run.model_copy(update={"status": "waiting", "metadata": run_metadata}))
        return refs

    def record_reconciliation_result(
        self,
        repair_id: str,
        *,
        descriptor_id: str,
        state: str,
        next_action: str,
        summary: str,
    ) -> None:
        """Project durable reconciliation evidence onto canonical Work/Run.

        Reconciliation is not verification and therefore never closes a
        Work/Run.  ``applied`` only clears the execution uncertainty so the
        service can run its deterministic verifier; every other state remains
        waiting and explicitly requires further observation or policy.
        """

        work = self.runtime.store.get_work(f"work_legacy_{repair_id}")
        run = self.runtime.store.get_run(f"run_legacy_{repair_id}")
        if work is None or run is None:
            return
        now = utcnow()
        unresolved = state != "applied"
        common = {
            "reconciliation_descriptor_id": descriptor_id,
            "reconciliation_state": state,
            "reconciliation_status": "required" if unresolved else "applied",
            "reconciliation_next_action": next_action,
            "reconciliation_required": unresolved,
            "reconciliation_reason": summary[:4_000],
        }
        work_metadata = dict(work.metadata)
        work_metadata.update(common)
        run_metadata = dict(run.metadata)
        run_metadata.update(common)
        self.runtime.store.save_work(
            work.model_copy(update={"status": "waiting", "metadata": work_metadata, "updated_at": now})
        )
        self.runtime.store.save_run(run.model_copy(update={"status": "waiting", "metadata": run_metadata}))

    def _grant(
        self,
        repair_id: str,
        work: Work,
        resource: str,
        version_ref: str,
        decision_id: str,
    ) -> AuthorizationGrant:
        version_token = self._stable_token(version_ref)
        grant_epoch = int(datetime.now(UTC).timestamp()) // 3600
        grant_id = f"authz_{repair_id}_{version_token}_{grant_epoch}"
        getter = getattr(self.runtime.store, "get_authorization", None)
        existing = getter(grant_id) if callable(getter) else None
        if isinstance(existing, AuthorizationGrant):
            from portable_runtime.records.authorization import is_grant_valid

            if is_grant_valid(existing):
                return existing
        grant = AuthorizationGrant(
            id=grant_id,
            principal_ref=self.principal_ref,
            grantee_ref=self.actor_ref,
            allowed_capabilities=["code.edit"],
            resource_scope=[resource],
            effect_ceiling="write-local",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            subject_version_refs=[version_ref],
            source_decision_ref=decision_id,
            metadata={
                "authority": "personal-owner-policy",
                "source": "control-plane-repair",
                "work_id": work.id,
            },
        )
        save_authorization = getattr(self.runtime.store, "save_authorization", None)
        if not callable(save_authorization):
            raise RuntimeError("portable authority store cannot persist AuthorizationGrant")
        save_authorization(grant)
        return grant

    def record_human_approval(
        self,
        repair_id: str,
        *,
        decided_by: str,
        principal_ref: str | None = None,
        principal_source: str = "request",
        action: str = "approve",
        note: str = "",
        operation_specs: Sequence[Mapping[str, Any]] | None = None,
    ) -> tuple[Decision, list[AuthorizationGrant]]:
        """Persist the canonical provenance for a human repair decision.

        The legacy approval row is only a compatibility projection.  A real
        approval is represented by one stable ``Decision`` and one or more
        grants whose capability, resource, and immutable subject version are
        copied from the operation that is about to run.  Grants are therefore
        never minted by :meth:`invoke_operation`; an operation can execute
        only after this method has established the human decision and its
        scoped grants.

        The identifiers are deterministic per repair/decision/spec so API
        retries and post-restart recovery are idempotent.  A conflicting
        second decision for the same repair is rejected rather than silently
        widening or replacing the original approval.
        """

        if action not in {"approve", "reject", "rollback"}:
            raise ValueError(f"unsupported human approval action: {action}")
        work_id = f"work_legacy_{repair_id}"
        run_id = f"run_legacy_{repair_id}"
        work = self.runtime.store.get_work(work_id)
        run = self.runtime.store.get_run(run_id)
        if work is None or run is None:
            raise RuntimeError(f"human approval requires canonical Work/Run for {repair_id}")
        raw_principal = (principal_ref or decided_by).strip()
        actor = raw_principal if ":" in raw_principal else f"human:{raw_principal or 'unknown'}"
        decision_id = f"decision_{repair_id}_human_approval"
        get_decision = getattr(self.runtime.store, "get_decision", None)
        existing_decision = get_decision(decision_id) if callable(get_decision) else None
        if existing_decision is not None:
            if (
                getattr(existing_decision, "selected_option", None) != action
                or actor not in list(getattr(existing_decision, "authorized_by", []))
            ):
                raise RuntimeError(f"conflicting human approval already recorded for {repair_id}")
            decision = existing_decision
        else:
            decision = Decision(
                id=decision_id,
                work_id=work.id,
                decision_type="human-approval",
                selected_option=action,
                authorized_by=[actor],
                metadata={
                    "repair_id": repair_id,
                    "decided_by": decided_by,
                    "principal_ref": actor,
                    "principal_source": principal_source,
                    "note": note[:4_000],
                    "source": "control-plane.approval",
                },
            )
            save_decision = getattr(self.runtime.store, "save_decision", None)
            if not callable(save_decision):
                raise RuntimeError("portable approval store cannot persist Decision")
            save_decision(decision)

        grants: list[AuthorizationGrant] = []
        specs = list(operation_specs or []) if action == "approve" else []
        if action == "approve" and not specs:
            resource = str(work.metadata.get("resource_ref") or work.metadata.get("resource_scope") or "")
            versions = [str(value) for value in work.metadata.get("subject_version_refs", [])]
            if resource and versions:
                specs = [
                    {
                        "capability": "git.merge",
                        "resource_ref": resource,
                        "subject_version_refs": versions,
                        "effect_class": "write-local",
                    },
                    {
                        "capability": "git.push",
                        "resource_ref": resource,
                        "subject_version_refs": versions,
                        "effect_class": "write-remote",
                    },
                ]
        if action == "approve" and not specs:
            raise RuntimeError("human approval has no capability/resource/version scope")

        existing_refs = list(work.metadata.get("human_approval_grant_refs", []))
        for raw_spec in specs:
            capability = str(raw_spec.get("capability") or "").strip()
            resource_ref = str(raw_spec.get("resource_ref") or "").strip()
            versions = [str(value).strip() for value in raw_spec.get("subject_version_refs", []) if str(value).strip()]
            effect_class = str(raw_spec.get("effect_class") or "write-local")
            if not capability or not resource_ref or not versions:
                raise ValueError("human approval grant requires capability, resource_ref, and subject_version_refs")
            token = self._stable_token(
                f"{decision.id}:{capability}:{resource_ref}:{','.join(versions)}:{effect_class}"
            )
            grant_id = f"authz_{repair_id}_human_{token}"
            get_authorization = getattr(self.runtime.store, "get_authorization", None)
            existing = get_authorization(grant_id) if callable(get_authorization) else None
            if isinstance(existing, AuthorizationGrant):
                grant = existing
            else:
                grant = AuthorizationGrant(
                    id=grant_id,
                    principal_ref=actor,
                    grantee_ref=self.actor_ref,
                    allowed_capabilities=[capability],
                    resource_scope=[resource_ref],
                    effect_ceiling=effect_class,
                    expires_at=datetime.now(UTC) + timedelta(hours=1),
                    subject_version_refs=versions,
                    source_decision_ref=decision.id,
                    metadata={
                        "authority": "human-approval",
                        "source": "control-plane.approval",
                        "repair_id": repair_id,
                        "decided_by": decided_by,
                        "principal_ref": actor,
                        "principal_source": principal_source,
                        "note": note[:4_000],
                    },
                )
                save_authorization = getattr(self.runtime.store, "save_authorization", None)
                if not callable(save_authorization):
                    raise RuntimeError("portable approval store cannot persist AuthorizationGrant")
                save_authorization(grant)
            grants.append(grant)
            if grant.id not in existing_refs:
                existing_refs.append(grant.id)

        work_metadata = dict(work.metadata)
        work_authorization_refs = list(work_metadata.get("authorization_refs", []))
        work_authorization_ids = {
            str(ref.get("id")) if isinstance(ref, dict) else str(ref)
            for ref in work_authorization_refs
        }
        for grant in grants:
            if grant.id not in work_authorization_ids:
                work_authorization_refs.append({"id": grant.id, "kind": "authorization"})
                work_authorization_ids.add(grant.id)
        work_metadata.update(
            {
                "human_approval_decision_ref": decision.id,
                "human_approval_action": action,
                "human_approval_decided_by": decided_by,
                "human_approval_principal_ref": actor,
                "human_approval_principal_source": principal_source,
                "human_approval_note": note[:4_000],
                "human_approval_grant_refs": existing_refs,
                "authorization_refs": work_authorization_refs,
            }
        )
        self.runtime.store.save_work(work.model_copy(update={"metadata": work_metadata, "updated_at": utcnow()}))
        run_metadata = dict(run.metadata)
        run_authorization_refs = list(run_metadata.get("authorization_refs", []))
        run_authorization_ids = {
            str(ref.get("id")) if isinstance(ref, dict) else str(ref)
            for ref in run_authorization_refs
        }
        for grant in grants:
            if grant.id not in run_authorization_ids:
                run_authorization_refs.append({"id": grant.id, "kind": "authorization"})
                run_authorization_ids.add(grant.id)
        run_metadata.update(
            {
                "human_approval_decision_ref": decision.id,
                "human_approval_action": action,
                "human_approval_grant_refs": existing_refs,
                "authorization_refs": run_authorization_refs,
            }
        )
        self.runtime.store.save_run(run.model_copy(update={"metadata": run_metadata}))
        return decision, grants

    def _prepare_qualification(
        self,
        *,
        repair_id: str,
        work: Work,
        run: Run,
        resource: str,
        version_ref: str,
        prompt: str,
    ) -> tuple[Work, Run, AuthorizationGrant]:
        token = self._stable_token(version_ref)
        failure_stop = self._record(
            BaseRecord(
                id=f"record_{repair_id}_{token}_failure_stop",
                record_type="Policy",
                lifecycle_status="candidate",
                metadata={
                    "qualification_kind": "failure-stop",
                    "condition": "provider failure or postcondition failure stops the repair",
                    "target_refs": [work.id],
                    "source": "personal-policy",
                },
            )
        )
        # A pre-execution assertion is deliberately scoped to the source
        # version.  The legacy verifier remains responsible for the post-edit
        # verification before the repair can close.
        baseline = self._record(
            BaseRecord(
                id=f"record_{repair_id}_{token}_baseline",
                record_type="Assertion",
                lifecycle_status="current",
                epistemic_status="supported",
                metadata={
                    "qualification_kind": "verification",
                    "result": "pass",
                    "phase": "precondition",
                    "target_refs": [work.id],
                    "subject_version_refs": [version_ref],
                    "source": "git-version-resolution",
                },
            )
        )
        evidence = self._record(
            BaseRecord(
                id=f"record_{repair_id}_{token}_evidence",
                record_type="EvidenceArtifact",
                lifecycle_status="current",
                metadata={
                    "qualification_kind": "evidence",
                    "target_refs": [work.id],
                    "subject_version_refs": [version_ref],
                    "source": "git-version-resolution",
                    "summary": "candidate workspace resolved to the requested git version",
                },
            )
        )
        relation_id = f"relation_{repair_id}_{token}_evidence"
        get_relation = getattr(self.runtime.store, "get_relation", None)
        relation = get_relation(relation_id) if callable(get_relation) else None
        if not isinstance(relation, RecordRelation):
            relation = RecordRelation(
                id=relation_id,
                relation_type="validated-under",
                subject_ref=evidence.id,
                object_ref=baseline.id,
                scope={"work_id": work.id, "subject_version_ref": version_ref},
                created_by=self.actor_ref,
                metadata={"qualification_kind": "relation"},
            )
            save_relation = getattr(self.runtime.store, "save_relation", None)
            if not callable(save_relation):
                raise RuntimeError("portable authority store cannot persist qualification relation")
            save_relation(relation)
        decision_id = f"decision_{repair_id}_{token}"
        decision = Decision(
            id=decision_id,
            work_id=work.id,
            decision_type="personal-owner-policy",
            selected_option="execute-local-edit",
            rationale_artifact_refs=[baseline.id],
            authorized_by=[self.principal_ref],
        )
        save_decision = getattr(self.runtime.store, "save_decision", None)
        get_decision = getattr(self.runtime.store, "get_decision", None)
        if callable(save_decision) and (not callable(get_decision) or get_decision(decision_id) is None):
            save_decision(decision)
        grant = self._grant(repair_id, work, resource, version_ref, decision_id)
        checkpoint = Checkpoint(
            id=f"checkpoint_{repair_id}_{token}",
            run_id=run.id,
            payload={
                "resource": resource,
                "subject_version_ref": version_ref,
                "prompt_digest": self._stable_token(prompt),
            },
        )
        get_checkpoint = getattr(self.runtime.store, "get_checkpoint", None)
        if not callable(get_checkpoint) or get_checkpoint(checkpoint.id) is None:
            self.runtime.store.save_checkpoint(checkpoint)

        procedure_refs = [
            {"id": failure_stop.id, "kind": "failure-stop"},
            {"id": baseline.id, "kind": "verification"},
            {"id": evidence.id, "kind": "evidence"},
            {"id": relation.id, "kind": "relation"},
            {"id": decision.id, "kind": "decision"},
            {"id": checkpoint.id, "kind": "checkpoint"},
        ]
        metadata = dict(work.metadata)
        metadata.update(
            {
                "purpose": prompt[:1000] or f"repair {repair_id}",
                "execution_boundary": "portable-runtime:RealityBoundary",
                "resource_ref": resource,
                "resource_scope": resource,
                "subject_version_refs": [version_ref],
                "actor_ref": self.actor_ref,
                "candidate": True,
                "procedure_profile": "standard",
                "procedure_profile_source": "code.edit-contract-minimum",
                "procedure_proof_refs": procedure_refs,
                "authorization_refs": [{"id": grant.id, "kind": "authorization"}],
                "authorization_grant_id": grant.id,
                "checkpoint_refs": [{"id": checkpoint.id, "kind": "checkpoint"}],
                "portable_authority": "personal-runtime",
            }
        )
        work = work.model_copy(update={"status": "running", "metadata": metadata, "updated_at": utcnow()})
        self.runtime.store.save_work(work)
        run_metadata = dict(run.metadata)
        run_metadata.update(
            {
                "actor_ref": self.actor_ref,
                "resource_ref": resource,
                "subject_version_refs": [version_ref],
                "procedure_profile": "standard",
                "procedure_proof_refs": procedure_refs,
                "authorization_refs": [{"id": grant.id, "kind": "authorization"}],
                "authorization_grant_id": grant.id,
                "checkpoint_refs": [{"id": checkpoint.id, "kind": "checkpoint"}],
                "portable_authority": "personal-runtime",
            }
        )
        # ``waiting`` is an explicit pre-execution state: the result is not
        # claimed yet and the post-edit verifier must close the legacy repair.
        run = run.model_copy(update={"status": "waiting", "metadata": run_metadata})
        self.runtime.store.save_run(run)
        return work, run, grant

    async def prepare_code_edit(self, *, repair_id: str, repo: str, prompt: str) -> tuple[Work, Run, str, str]:
        row = self._legacy_row(repair_id)
        work, run = dual_write_repair(row, self.runtime.store)
        version_ref = await self._version_ref(repo)
        resource = f"repo:{Path(repo).resolve()}"
        work, run, _grant = self._prepare_qualification(
            repair_id=repair_id,
            work=work,
            run=run,
            resource=resource,
            version_ref=version_ref,
            prompt=prompt,
        )
        return work, run, resource, version_ref

    def ensure_repair_projection(
        self,
        *,
        repair_id: str,
        fingerprint: str,
        payload_json: str,
        attempt: int,
    ) -> tuple[Work, Run]:
        """Materialise canonical alert state before the legacy projection.

        This is intentionally version-neutral: the git subject version and
        owner grant are added later, immediately before ``code.edit`` once the
        repository has been selected.  The stable Work/Run ids make retries
        idempotent and let startup recovery find the canonical work even if
        the legacy insert fails halfway through.
        """

        work, run = dual_write_repair(
            {
                "id": repair_id,
                "fingerprint": fingerprint,
                "payload_json": payload_json,
                "status": "open",
                "attempt": attempt,
            },
            self.runtime.store,
        )
        metadata = dict(work.metadata)
        metadata.update(
            {
                "legacy_repair_id": repair_id,
                "legacy_fingerprint": fingerprint,
                "alert_payload_source": "control-plane.alertmanager",
                "attempt": attempt,
                "canonical_authority": "portable-runtime",
            }
        )
        work = work.model_copy(update={"status": "open", "metadata": metadata, "updated_at": utcnow()})
        self.runtime.store.save_work(work)
        run = run.model_copy(update={"status": "queued", "metadata": {**run.metadata, "attempt": attempt}})
        self.runtime.store.save_run(run)
        return work, run

    async def invoke(
        self,
        *,
        repair_id: str,
        repo: str,
        prompt: str,
        capability: str = "code.edit",
        model: str | None = None,
        timeout_seconds: float | None = None,
    ) -> CapabilityResult:
        if capability == "reason.generate":
            request = CapabilityRequest(
                id=f"req-{repair_id}-reason",
                capability=capability,
                instruction=prompt,
                parameters={"prompt": prompt, "repo": repo, "model": model or ""},
                timeout_seconds=timeout_seconds,
                metadata={"portable_authority": "personal-runtime"},
            )
            return await self.runtime.capabilities.invoke(request)

        work, run, resource, version_ref = await self.prepare_code_edit(
            repair_id=repair_id,
            repo=repo,
            prompt=prompt,
        )
        request = CapabilityRequest(
            id=f"req-{repair_id}-{self._stable_token(version_ref)}",
            capability=capability,
            work_id=work.id,
            run_id=run.id,
            instruction=prompt,
            parameters={"prompt": prompt, "repo": repo, "model": model or ""},
            resource_ref=resource,
            subject_version_refs=[version_ref],
            actor_ref=self.actor_ref,
            effect_class="write-local",
            idempotency_key=f"{work.id}:code.edit:{version_ref}",
            timeout_seconds=timeout_seconds,
            metadata={
                "portable_authority": "personal-runtime",
                "procedure_profile": "standard",
                "resource_ref": resource,
                "subject_version_refs": [version_ref],
                "actor_ref": self.actor_ref,
            },
        )
        result = await self.runtime.capabilities.invoke(request)
        final_work = self.runtime.store.get_work(work.id)
        final_run = self.runtime.store.get_run(run.id)
        if final_work is not None and final_run is not None:
            self._persist_execution_outcome(final_work, final_run, result)
        return result

    def finalize_repair(
        self,
        repair_id: str,
        *,
        verified: bool,
        verification_refs: list[str] | None = None,
        summary: str = "",
    ) -> None:
        """Close canonical Work/Run only through typed verification proofs.

        ``verified`` is retained as a compatibility discriminator for the
        legacy caller, but it is never authority.  A successful finalization
        must provide durable proof records bound to this exact Work/Run,
        subject versions, verification scope and acceptance criteria.  The
        portable ``CompletionAuthority`` owns the terminal Run transition;
        this adapter only projects that decision to Work and the legacy row.
        """

        work_id = f"work_legacy_{repair_id}"
        run_id = f"run_legacy_{repair_id}"
        work = self.runtime.store.get_work(work_id)
        run = self.runtime.store.get_run(run_id)
        if work is None or run is None:
            return
        now = datetime.now(UTC)
        refs = list(
            verification_refs
            if verification_refs is not None
            else work.metadata.get("verification_refs", [])
        )
        if not refs:
            raise ValueError("repair finalization requires durable verification proof refs")

        proof_result = "pass" if verified else "fail"
        for ref in refs:
            record = self.runtime.store.get_record(str(ref))
            relation_getter = getattr(self.runtime.store, "get_relation", None)
            relation = relation_getter(str(ref)) if callable(relation_getter) else None
            if record is None and relation is not None:
                if (
                    getattr(relation, "relation_type", None) != "validated-under"
                    or getattr(relation, "scope", {}).get("work_id") != work.id
                    or getattr(relation, "scope", {}).get("run_id") != run.id
                ):
                    raise ValueError(f"repair finalization relation proof {ref!r} is not bound to this Work/Run")
                continue
            if record is None:
                raise ValueError(f"repair finalization proof {ref!r} is not durable")
            metadata = getattr(record, "metadata", {})
            result = metadata.get("verification_result") if isinstance(metadata, dict) else None
            if not isinstance(result, dict) or str(result.get("result", "")).lower() != proof_result:
                raise ValueError(
                    f"repair finalization proof {ref!r} does not contain an explicit {proof_result} result"
                )
            if str(result.get("work_id", "")) != work.id or str(result.get("run_id", "")) != run.id:
                raise ValueError(f"repair finalization proof {ref!r} is bound to a different Work/Run")
            expected_versions = list(work.metadata.get("subject_version_refs", []))
            actual_versions = result.get("subject_version_refs")
            if actual_versions != expected_versions:
                raise ValueError(f"repair finalization proof {ref!r} has an incompatible subject version scope")
            expected_scope = str(work.metadata.get("verification_scope") or work.kind)
            if str(result.get("scope", "")) != expected_scope:
                raise ValueError(f"repair finalization proof {ref!r} has an incompatible verification scope")
            criteria_digest = self._verification_criteria_digest(work)
            if str(result.get("criteria_digest", "")) != criteria_digest:
                raise ValueError(f"repair finalization proof {ref!r} has an incompatible acceptance-criteria scope")

        if verified:
            # The shared portable authority is the only owner allowed to
            # transition a Run to ``succeeded``.  It resolves each proof from
            # the durable semantic store and rejects provider/status hints.
            from portable_runtime.workflows.completion import CompletionAuthority

            # ``verification_refs`` is a complete provenance bundle (assertion,
            # evidence and relation).  The portable completion primitive only
            # consumes durable typed records, so pass the EvidenceArtifact
            # proof(s) while retaining the complete bundle on Work/Run.
            completion_refs = [
                str(ref)
                for ref in refs
                if getattr(self.runtime.store.get_record(str(ref)), "record_type", None)
                == "EvidenceArtifact"
            ]
            if not completion_refs:
                raise ValueError("repair finalization requires a typed EvidenceArtifact proof")
            CompletionAuthority(self.runtime.store).authorize(
                work=work,
                run=run,
                verification_refs=completion_refs,
            )
        else:
            # A failing typed verification is still a durable terminal fact,
            # but it is not authorized by the success CompletionAuthority.
            self.runtime.store.save_run(
                run.model_copy(update={"status": "failed", "ended_at": now})
            )
        work_metadata = dict(work.metadata)
        work_metadata.update({"verification_refs": refs, "verification_summary": summary, "verified": verified})
        run_metadata = dict(run.metadata)
        run_metadata.update({"verification_refs": refs, "verification_summary": summary, "verified": verified})
        self.runtime.store.save_work(
            work.model_copy(
                update={
                    "status": "completed" if verified else "failed",
                    "metadata": work_metadata,
                    "updated_at": now,
                }
            )
        )
        final_run = self.runtime.store.get_run(run.id)
        if final_run is None:
            raise RuntimeError("portable completion authority did not persist the canonical Run")
        self.runtime.store.save_run(
            final_run.model_copy(
                update={
                    "metadata": run_metadata,
                    "ended_at": now,
                }
            )
        )

    @staticmethod
    def _verification_criteria_digest(work: Work) -> str:
        """Return a stable digest for the Work's acceptance criteria."""

        import json

        criteria = work.acceptance_criteria or ["deterministic-verification"]
        payload = json.dumps(criteria, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def record_verification(
        self,
        repair_id: str,
        *,
        passed: bool,
        summary: str,
        evidence_refs: list[str] | None = None,
        verifier_ref: str = "control-plane.deterministic-verifier",
    ) -> list[str]:
        """Persist deterministic verifier output before canonical closure."""

        work = self.runtime.store.get_work(f"work_legacy_{repair_id}")
        run = self.runtime.store.get_run(f"run_legacy_{repair_id}")
        if work is None or run is None:
            return []
        token = self._stable_token(f"verification:{repair_id}:{summary}")
        version_refs = list(work.metadata.get("subject_version_refs", []))
        verification_scope = str(work.metadata.get("verification_scope") or work.kind)
        criteria_digest = self._verification_criteria_digest(work)
        verification_result = {
            "result": "pass" if passed else "fail",
            "work_id": work.id,
            "run_id": run.id,
            "scope": verification_scope,
            "subject_version_refs": version_refs,
            "criteria_digest": criteria_digest,
            "verifier_ref": verifier_ref,
        }
        verification = self._record(
            BaseRecord(
                id=f"record_{repair_id}_{token}_verification",
                record_type="Assertion",
                lifecycle_status="current",
                epistemic_status="supported" if passed else "contested",
                metadata={
                    "qualification_kind": "verification",
                    "result": "pass" if passed else "fail",
                    "target_refs": [work.id, run.id],
                    "subject_version_refs": version_refs,
                    "verifier_ref": verifier_ref,
                    "summary": summary[:4_000],
                    "verification_result": verification_result,
                    "proof_kind": "closed-verification",
                },
            )
        )
        evidence = self._record(
            BaseRecord(
                id=f"record_{repair_id}_{token}_verification_evidence",
                record_type="EvidenceArtifact",
                lifecycle_status="current",
                metadata={
                    "qualification_kind": "evidence",
                    "target_refs": [work.id, run.id],
                    "subject_version_refs": version_refs,
                    "verifier_ref": verifier_ref,
                    "evidence_refs": list(evidence_refs or []),
                    "summary": summary[:4_000],
                    "verification_result": verification_result,
                    "proof_kind": "closed-verification",
                },
            )
        )
        relation_id = f"relation_{repair_id}_{token}_verification"
        get_relation = getattr(self.runtime.store, "get_relation", None)
        relation = get_relation(relation_id) if callable(get_relation) else None
        if not isinstance(relation, RecordRelation):
            relation = RecordRelation(
                id=relation_id,
                relation_type="validated-under",
                subject_ref=evidence.id,
                object_ref=verification.id,
                scope={"work_id": work.id, "run_id": run.id},
                created_by=verifier_ref,
                metadata={"qualification_kind": "relation"},
            )
            save_relation = getattr(self.runtime.store, "save_relation", None)
            if not callable(save_relation):
                raise RuntimeError("portable authority store cannot persist verification relation")
            save_relation(relation)
        refs = [verification.id, evidence.id, relation.id]
        verification_status = "passed" if passed else "failed"
        work_metadata = dict(work.metadata)
        work_metadata.update(
            {
                "verification_refs": refs,
                "verification_status": verification_status,
                "verification_summary": summary[:4_000],
            }
        )
        run_metadata = dict(run.metadata)
        run_metadata.update(
            {
                "verification_refs": refs,
                "verification_status": verification_status,
                "verification_summary": summary[:4_000],
            }
        )
        self.runtime.store.save_work(work.model_copy(update={"metadata": work_metadata, "updated_at": utcnow()}))
        self.runtime.store.save_run(run.model_copy(update={"metadata": run_metadata}))
        return refs

    async def invoke_operation(
        self,
        *,
        repair_id: str,
        capability: str,
        resource_ref: str,
        parameters: dict[str, Any],
        effect_class: str,
        subject_version_refs: list[str] | None = None,
        instruction: str | None = None,
    ) -> CapabilityResult:
        """Invoke a non-Codex personal operation through the Runtime.

        Git merge/push and Docker lifecycle actions use the same Work/Run as
        the repair, but must consume a capability-scoped grant previously
        materialised by :meth:`record_human_approval`.  This method is not an
        authority to mint personal-owner-policy grants: missing or mismatched
        human provenance fails closed before a provider is selected.  A
        successful provider invocation leaves the Run in ``waiting``;
        deterministic repair verification owns final closure.
        """

        work_id = f"work_legacy_{repair_id}"
        run_id = f"run_legacy_{repair_id}"
        work = self.runtime.store.get_work(work_id)
        run = self.runtime.store.get_run(run_id)
        if work is None or run is None:
            raise RuntimeError(f"portable operation requires canonical Work/Run for {repair_id}")
        versions = list(subject_version_refs or [])
        if not versions:
            return CapabilityResult(
                request_id=f"req-{repair_id}-{capability.replace('.', '-')}",
                provider_id="portable-runtime-authority",
                status="failed",
                message="personal operation requires an immutable subject version",
                error={"code": "SubjectVersionRequired", "repair_id": repair_id, "capability": capability},
            )
        decision_id = str(work.metadata.get("human_approval_decision_ref") or "")
        get_decision = getattr(self.runtime.store, "get_decision", None)
        decision = get_decision(decision_id) if decision_id and callable(get_decision) else None
        if (
            decision is None
            or getattr(decision, "decision_type", None) != "human-approval"
            or getattr(decision, "selected_option", None) != "approve"
        ):
            return CapabilityResult(
                request_id=f"req-{repair_id}-{capability.replace('.', '-')}",
                provider_id="portable-runtime-authority",
                status="failed",
                message="human approval provenance is required before personal operation",
                error={"code": "HumanApprovalRequired", "repair_id": repair_id, "capability": capability},
            )
        grant_refs = [str(ref) for ref in work.metadata.get("human_approval_grant_refs", [])]
        grants: list[AuthorizationGrant] = []
        get_authorization = getattr(self.runtime.store, "get_authorization", None)
        if callable(get_authorization):
            for ref in grant_refs:
                candidate = get_authorization(ref)
                if isinstance(candidate, AuthorizationGrant):
                    grants.append(candidate)
        matching = [
            grant
            for grant in grants
            if capability in grant.allowed_capabilities
            and resource_ref in grant.resource_scope
            and grant.subject_version_refs == versions
            and grant.source_decision_ref == decision.id
        ]
        if not matching:
            return CapabilityResult(
                request_id=f"req-{repair_id}-{capability.replace('.', '-')}",
                provider_id="portable-runtime-authority",
                status="failed",
                message="human approval grant does not cover this operation scope",
                error={
                    "code": "HumanApprovalScopeMismatch",
                    "repair_id": repair_id,
                    "capability": capability,
                    "resource_ref": resource_ref,
                    "subject_version_refs": versions,
                },
            )
        grant = matching[0]
        token = self._stable_token(f"{capability}:{resource_ref}:{','.join(versions)}")
        procedure_refs = list(work.metadata.get("procedure_proof_refs", []))
        if not any(
            (ref.get("id") if isinstance(ref, dict) else ref) == decision.id
            for ref in procedure_refs
        ):
            procedure_refs.append({"id": decision.id, "kind": "decision"})
        work_metadata = dict(work.metadata)
        work_metadata.update(
            {
                "authorization_refs": [
                    *work_metadata.get("authorization_refs", []),
                    {"id": grant.id, "kind": "authorization"},
                ],
                "authorization_grant_id": grant.id,
                "procedure_proof_refs": procedure_refs,
                "operation_capability": capability,
                "operation_resource_ref": resource_ref,
            }
        )
        self.runtime.store.save_work(
            work.model_copy(
                update={"status": "waiting", "metadata": work_metadata, "updated_at": utcnow()}
            )
        )
        run_metadata = dict(run.metadata)
        run_metadata.update(
            {
                "authorization_refs": [
                    *run_metadata.get("authorization_refs", []),
                    {"id": grant.id, "kind": "authorization"},
                ],
                "authorization_grant_id": grant.id,
                "procedure_proof_refs": procedure_refs,
                "procedure_profile": "standard",
                "operation_capability": capability,
                "operation_resource_ref": resource_ref,
            }
        )
        self.runtime.store.save_run(run.model_copy(update={"status": "waiting", "metadata": run_metadata}))
        request = CapabilityRequest(
            id=f"req-{repair_id}-{capability.replace('.', '-')}-{token}",
            capability=capability,
            work_id=work.id,
            run_id=run.id,
            instruction=instruction or capability,
            parameters=dict(parameters),
            resource_ref=resource_ref,
            subject_version_refs=versions,
            actor_ref=self.actor_ref,
            effect_class=effect_class,  # type: ignore[arg-type]
            idempotency_key=f"{work.id}:{capability}:{token}",
            metadata={
                "portable_authority": "personal-runtime",
                "procedure_profile": "standard",
                "resource_ref": resource_ref,
                "subject_version_refs": versions,
                "actor_ref": self.actor_ref,
                "authorization_refs": [{"id": grant.id, "kind": "authorization"}],
                "authorization_grant_id": grant.id,
                "procedure_proof_refs": procedure_refs,
            },
        )
        result = await self.runtime.capabilities.invoke(request)
        final_work = self.runtime.store.get_work(work.id)
        final_run = self.runtime.store.get_run(run.id)
        if final_work is not None and final_run is not None:
            self._persist_execution_outcome(final_work, final_run, result)
        return result
