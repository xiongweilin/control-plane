"""Deep adapter from the legacy repair API to the portable runtime authority.

The adapter owns the migration seam.  Callers provide only a repair id, a
repository and a prompt; this module materialises the canonical Work/Run,
binds the request to the current git version, records the personal-owner
grant, and invokes the portable ``RealityBoundary``.  The legacy SQLite row is
read only as migration input and remains a compatibility projection.
"""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from portable_runtime.core.capabilities import CapabilityRequest, CapabilityResult
from portable_runtime.core.models import Checkpoint, Decision, Run, Work, utcnow
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
        principal_ref: str = "personal-owner",
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
        final_run = self.runtime.store.get_run(run.id)
        if final_run is not None:
            status = "waiting" if result.status == "succeeded" else "failed"
            update: dict[str, Any] = {"status": status}
            if status == "failed":
                update["ended_at"] = datetime.now(UTC)
            self.runtime.store.save_run(final_run.model_copy(update=update))
        final_work = self.runtime.store.get_work(work.id)
        if final_work is not None:
            status = "waiting" if result.status == "succeeded" else "failed"
            self.runtime.store.save_work(final_work.model_copy(update={"status": status, "updated_at": utcnow()}))
        return result

    def finalize_repair(
        self,
        repair_id: str,
        *,
        verified: bool,
        verification_refs: list[str] | None = None,
        summary: str = "",
    ) -> None:
        """Close canonical Work/Run only after deterministic verification."""

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
        self.runtime.store.save_run(
            run.model_copy(
                update={
                    "status": "succeeded" if verified else "failed",
                    "metadata": run_metadata,
                    "ended_at": now,
                }
            )
        )

    def record_verification(
        self,
        repair_id: str,
        *,
        passed: bool,
        summary: str,
        evidence_refs: list[str] | None = None,
    ) -> list[str]:
        """Persist deterministic verifier output before canonical closure."""

        work = self.runtime.store.get_work(f"work_legacy_{repair_id}")
        run = self.runtime.store.get_run(f"run_legacy_{repair_id}")
        if work is None or run is None:
            return []
        token = self._stable_token(f"verification:{repair_id}:{summary}")
        version_refs = list(work.metadata.get("subject_version_refs", []))
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
                    "verifier_ref": "control-plane.deterministic-verifier",
                    "summary": summary[:4_000],
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
                    "verifier_ref": "control-plane.deterministic-verifier",
                    "evidence_refs": list(evidence_refs or []),
                    "summary": summary[:4_000],
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
                created_by="control-plane.deterministic-verifier",
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
        the repair, but receive a separate capability-scoped Decision and
        AuthorizationGrant.  A successful provider invocation leaves the Run
        in ``waiting``; deterministic repair verification owns final closure.
        """

        work_id = f"work_legacy_{repair_id}"
        run_id = f"run_legacy_{repair_id}"
        work = self.runtime.store.get_work(work_id)
        run = self.runtime.store.get_run(run_id)
        if work is None or run is None:
            raise RuntimeError(f"portable operation requires canonical Work/Run for {repair_id}")
        versions = list(subject_version_refs or [])
        token = self._stable_token(f"{capability}:{resource_ref}:{','.join(versions)}")
        decision_id = f"decision_{repair_id}_{capability.replace('.', '_')}_{token}"
        decision = Decision(
            id=decision_id,
            work_id=work.id,
            decision_type="personal-operation-authorization",
            selected_option=capability,
            rationale_artifact_refs=[
                str(ref.get("id")) if isinstance(ref, dict) else str(ref)
                for ref in work.metadata.get("procedure_proof_refs", [])
            ],
            authorized_by=[self.principal_ref],
        )
        save_decision = getattr(self.runtime.store, "save_decision", None)
        if not callable(save_decision):
            raise RuntimeError("portable operation store cannot persist Decision")
        save_decision(decision)
        grant_id = f"authz_{repair_id}_{capability.replace('.', '_')}_{token}"
        grant = AuthorizationGrant(
            id=grant_id,
            principal_ref=self.principal_ref,
            grantee_ref=self.actor_ref,
            allowed_capabilities=[capability],
            resource_scope=[resource_ref],
            effect_ceiling=effect_class,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            subject_version_refs=versions,
            source_decision_ref=decision.id,
            metadata={"authority": "personal-owner-policy", "operation": capability, "work_id": work.id},
        )
        save_authorization = getattr(self.runtime.store, "save_authorization", None)
        if not callable(save_authorization):
            raise RuntimeError("portable operation store cannot persist AuthorizationGrant")
        save_authorization(grant)
        procedure_refs = list(work.metadata.get("procedure_proof_refs", []))
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
        if result.status != "succeeded":
            final_run = self.runtime.store.get_run(run.id)
            if final_run is not None:
                self.runtime.store.save_run(
                    final_run.model_copy(update={"status": "failed", "ended_at": datetime.now(UTC)})
                )
            final_work = self.runtime.store.get_work(work.id)
            if final_work is not None:
                self.runtime.store.save_work(final_work.model_copy(update={"status": "failed", "updated_at": utcnow()}))
        return result
