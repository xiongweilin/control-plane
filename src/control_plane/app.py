from __future__ import annotations

import asyncio
import logging
import secrets
import time
import uuid
from contextlib import asynccontextmanager, suppress
from contextvars import ContextVar
from typing import Any

import httpx
from fastapi import FastAPI, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, REGISTRY, generate_latest
from pydantic import BaseModel

from .approvals import ApprovalManager
from .audit import inspect_session_fields
from .budget import Budget
from .codex_runner import CodexRunner
from .config import ControlPlaneConfig, canonical_human_principal
from .metrics import AUTH_FAILURES, ControlPlaneCollector
from .models import (
    AlertmanagerPayload,
    AlertPolicyRequest,
    AlertResponse,
    ApprovalDecisionRequest,
    ApprovalDecisionResponse,
    ControlRequest,
    ErrorDetail,
    ErrorResponse,
    TaskDispatchResponse,
    TaskRequest,
)
from .notify import Notifier
from .portable_authority import PortableRuntimeAuthority
from .runtime import (
    acquire_single_instance,
    bootstrap,
    graceful_shutdown,
    run_info_dict,
    with_run_id,
)
from .service import RepairService

# Portable runtime bridge (additive, section 14/31)
try:
    from portable_runtime.deployment.local import create_personal_platform_runtime
    _PORTABLE_AVAILABLE = True
except Exception:  # pragma: no cover
    _PORTABLE_AVAILABLE = False
    create_personal_platform_runtime = None  # type: ignore[assignment]
from .state_machine import RepairState
from .storage import Store

# 记录当前请求路径，供鉴权失败指标打标（中间件维护）
_current_path: ContextVar[str] = ContextVar("cp_current_path", default="unknown")

logger = logging.getLogger(__name__)

USAGE_HINT = (
    "控制平面使用提示：\n"
    "/cp status 控制平面状态\n"
    "/cp approve <repair_id> | reject | rollback 审批/回滚修复\n"
    "/cp policy <fingerprint> auto|manual|ignore 设置告警策略（自动/手动/忽略）\n"
    "/cp run <fingerprint> 手动策略下让模型执行修复\n"
    "/cp ignore <fingerprint> 忽略该告警\n"
    "/cp evidence 查看沉淀证据与候选\n"
    "/cp promote <candidate_id> 晋升候选经验\n"
    "/cp pause | resume 暂停/恢复控制平面\n"
    "/task <描述> 或直接发送任意消息：派发任务给 Agent 执行\n"
    "/status /alerts /help 网关只读状态\n"
    "修复过程会自动推送：开始修复 → Agent 启动 → 验证 → 完成/失败。"
)


class PromoteRequest(BaseModel):
    decided_by: str
    note: str = ""


class CleanupRequest(BaseModel):
    repos: list[str] = []
    apply: bool = False


def create_app(config: ControlPlaneConfig | None = None) -> FastAPI:
    cfg = with_run_id(config or ControlPlaneConfig.load())
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    cfg.patch_dir.mkdir(parents=True, exist_ok=True)
    cfg.evidence_dir.mkdir(parents=True, exist_ok=True)

    store = Store(cfg.state_db)
    # Portable runtime is the canonical authority; the legacy DB remains a
    # compatibility projection and is updated only after canonical writes.
    if not _PORTABLE_AVAILABLE or create_personal_platform_runtime is None:
        raise RuntimeError("portable runtime is required for the personal control-plane app")
    try:
        portable_runtime = create_personal_platform_runtime(cfg.data_dir / "portable-runtime.db")
        # The personal runtime owns provider registration.  The legacy
        # CodexRunner remains available only for compatibility commands;
        # alert-driven repair execution uses this provider through the
        # runtime's RealityBoundary.
        from portable_runtime.providers.codex.provider import CodexProvider

        from .codex_boundary import CodexExecutionBoundaryAdapter

        portable_runtime.registry.register(
            CodexProvider(
                model=cfg.model,
                cli=getattr(cfg, "codex_cli", None),
                timeout_seconds=float(
                    cfg.exec_timeout_seconds
                    or cfg.per_repair_timeout_seconds
                    or 900
                ),
                execution_boundary=CodexExecutionBoundaryAdapter(cfg),
            )
        )
    except Exception as exc:
        logger.exception("portable runtime bootstrap failed")
        raise RuntimeError("portable runtime bootstrap failed") from exc
    budget = Budget(store, cfg.daily_agent_budget, cfg.max_agent_calls_per_repair)
    approvals = ApprovalManager()
    notifier = Notifier(cfg)
    agent = CodexRunner(cfg)
    agent.attach_store(store)
    if portable_runtime is not None:
        store.attach_portable_store(portable_runtime.store, enable_read=True)
    service = RepairService(
        cfg,
        store,
        budget,
        agent,
        approvals,
        notifier,
        portable_runtime=portable_runtime,
    )
    # Personal runtime effects are explicit providers, never Codex prompt
    # permissions.  The contracts are registered on the private Runtime
    # instance so authorization/reliability/procedure gates run before Git or
    # Docker touches the host or a remote repository.
    from portable_runtime.core.capability_contract import CapabilityContract

    from .personal_operations import PersonalOperationsProvider
    from .reconciliation import ReconciliationDescriptorStore

    reconciliation_store = ReconciliationDescriptorStore(cfg.data_dir / "reconciliation.db")

    for contract in (
        CapabilityContract(
            capability="git.merge",
            minimum_impact_class="write-local",
            effect_semantics="idempotent",
            reversibility="reversible",
            authorization_requirement="required",
            minimum_procedure_profile="standard",
            resource_required=True,
            subject_version_required=True,
            blast_radius=1,
            exposure=1,
        ),
        CapabilityContract(
            capability="git.push",
            minimum_impact_class="write-remote",
            effect_semantics="reconcilable",
            reversibility="compensatable",
            authorization_requirement="required",
            minimum_procedure_profile="standard",
            resource_required=True,
            subject_version_required=True,
            blast_radius=2,
            exposure=2,
        ),
        CapabilityContract(
            capability="git.rollback",
            minimum_impact_class="write-local",
            effect_semantics="idempotent",
            reversibility="reversible",
            authorization_requirement="required",
            minimum_procedure_profile="standard",
            resource_required=True,
            subject_version_required=True,
            blast_radius=1,
            exposure=1,
        ),
        CapabilityContract(
            capability="docker.restart",
            minimum_impact_class="write-remote",
            effect_semantics="reconcilable",
            reversibility="compensatable",
            authorization_requirement="required",
            minimum_procedure_profile="standard",
            resource_required=True,
            subject_version_required=False,
            blast_radius=2,
            exposure=2,
        ),
        CapabilityContract(
            capability="docker.compose.up",
            minimum_impact_class="write-remote",
            effect_semantics="reconcilable",
            reversibility="compensatable",
            authorization_requirement="required",
            minimum_procedure_profile="standard",
            resource_required=True,
            subject_version_required=False,
            blast_radius=3,
            exposure=3,
        ),
    ):
        portable_runtime.contract_registry.register(contract)
    portable_runtime.registry.register(
        PersonalOperationsProvider(
            cfg,
            service.executor,
            reconciliation_store=reconciliation_store,
        )
    )
    with suppress(ValueError):
        # skip if already registered (e.g. test app created twice)
        REGISTRY.register(ControlPlaneCollector(store, budget.remaining))

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        run = bootstrap(cfg.pid_file)
        acquired, detail = acquire_single_instance(cfg.pid_file)
        if not acquired:
            logger.error("refusing to start: %s", detail)
            raise RuntimeError(detail)
        store.start_run_record(
            run.run_id,
            run.pid,
            run.hostname,
            run.python_version,
        )
        logger.info(
            "control plane started run_id=%s pid=%s hostname=%s (%s)",
            run.run_id,
            run.pid,
            run.hostname,
            detail,
        )
        expired = store.expire_candidates(int(time.time()))
        if expired:
            logger.info("expired %s candidates", expired)
        now = int(time.time())
        keep_pending = {RepairState.NEEDS_APPROVAL.value, RepairState.RECOVERING.value}
        for row in store.list_repairs_with_fallback(limit=1_000):
            if row["status"] in keep_pending:
                continue
            if row["status"] not in {"closed", "failed", "interrupted", "rolled_back"}:
                store.set_repair_status(
                    row["id"],
                    "interrupted",
                    error="control plane restarted",
                    finished_at=now,
                )
                logger.info("reconciled stale repair %s -> interrupted", row["id"])
        try:
            recovery = await service.reconcile_startup_descriptors(reconciliation_store)
            if recovery:
                logger.info("durable reconciliation done for %s descriptor(s)", len(recovery))
        except Exception:
            logger.exception("durable reconciliation failed")
        resumed = [
            row["id"]
            for row in store.list_repairs_with_fallback(limit=1_000)
            if row["status"] == RepairState.NEEDS_APPROVAL.value
        ]
        if not store.get_setting("usage_hint_sent"):
            await notifier.notify("info", "控制平面使用提示", USAGE_HINT)
            store.set_setting("usage_hint_sent", "1")
        model_task: asyncio.Task[Any] | None = None
        if cfg.model_preflight_enabled:
            try:
                preflight = await service.startup_model_preflight()
                if not preflight["ok"]:
                    logger.warning(
                        "model preflight degraded: %s",
                        "; ".join(preflight["problems"]),
                    )
                    model_task = asyncio.create_task(service.model_recovery_loop())
                else:
                    logger.info(
                        "model preflight ok (cli=%s, gateway=%s, model=%s)",
                        preflight["sources"]["cli"]["ok"],
                        preflight["sources"]["gateway"]["ok"],
                        preflight["sources"]["model"]["ok"],
                    )
            except Exception:
                logger.exception("model preflight failed")
                model_task = asyncio.create_task(service.model_recovery_loop())
        try:
            await service.reconcile_alerts()
            logger.info("alert reconciliation done")
        except Exception:
            logger.exception("alert reconciliation failed")
        digest_task = asyncio.create_task(service.digest_loop())
        scan_task = asyncio.create_task(service.scan_loop())
        resume_tasks = [asyncio.create_task(service.resume_pending_approval(r)) for r in resumed]
        if cfg.candidate_cleanup_policy == "auto":
            try:
                # ``auto`` is retained as a compatibility setting but is now
                # advisory-only.  Startup may enumerate retention candidates;
                # it must never erase a branch without owner authorization and
                # durable recovery evidence.
                candidates = await service.cleanup_candidate_branches(apply=False)
                if candidates:
                    logger.info(
                        "candidate-branch cleanup is advisory-only; %s stale branches require owner review",
                        len(candidates),
                    )
            except Exception:
                logger.exception("candidate-branch advisory scan failed")
        try:
            yield
        finally:
            scan_task.cancel()
            digest_task.cancel()
            if model_task is not None:
                model_task.cancel()
            for task in resume_tasks:
                task.cancel()
            await service.close()
            reconciliation_store.close()
            store.stop_run_record(run.run_id)
            graceful_shutdown(run.pid_file)
            store.close()

    app = FastAPI(
        title="Control Plane",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.service = service
    app.state.store = store
    app.state.portable_runtime = portable_runtime
    app.state.reconciliation_store = reconciliation_store

    @app.middleware("http")
    async def record_current_path(request: Request, call_next):
        token = _current_path.set(request.url.path)
        try:
            return await call_next(request)
        finally:
            _current_path.reset(token)

    def key_matches(candidate: str) -> bool:
        return bool(candidate) and secrets.compare_digest(candidate, cfg.api_key)

    async def require_key(x_control_plane_key: str = Header(default="")) -> None:
        if not key_matches(x_control_plane_key):
            AUTH_FAILURES.labels(reason="invalid_key", endpoint=_current_path.get()).inc()
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")

    async def valid_alertmanager_key(header_key: str, authorization: str) -> bool:
        if key_matches(header_key):
            return True
        scheme, separator, credential = authorization.partition(" ")
        return separator == " " and scheme.lower() == "bearer" and key_matches(credential)

    def error_payload(code: str, message: str) -> ErrorResponse:
        return ErrorResponse(error=ErrorDetail(code=code, message=message))

    @app.exception_handler(HTTPException)
    async def http_exception_handler(_: Request, exc: HTTPException) -> JSONResponse:
        body = ErrorResponse(
            error=ErrorDetail(code="HTTP_ERROR", message=str(exc.detail))
        )
        return JSONResponse(status_code=exc.status_code, content=body.model_dump())

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/live", include_in_schema=False)
    async def live() -> dict[str, Any]:
        """Liveness: the process itself is serving (no external dependencies)."""
        return {"status": "ok", **run_info_dict()}

    @app.get("/ready", include_in_schema=False)
    async def ready() -> Any:
        """Readiness: DB writable, prometheus/alertmanager reachable per config."""
        checks: dict[str, Any] = {}
        db_ok = store.check_writable()
        checks["database"] = {"ok": db_ok, "detail": "writable" if db_ok else "unavailable"}
        last_ready = store.get_setting("health:last_ready", "0")
        timeout = max(5, cfg.comm_timeout_seconds)
        async with httpx.AsyncClient(timeout=timeout) as client:
            if cfg.prometheus_url:
                try:
                    response = await client.get(f"{cfg.prometheus_url}/-/ready")
                    checks["prometheus"] = {
                        "ok": response.status_code == 200,
                        "detail": f"HTTP {response.status_code}",
                    }
                except httpx.HTTPError as exc:
                    checks["prometheus"] = {"ok": False, "detail": str(exc)}
            if cfg.alertmanager_url:
                try:
                    response = await client.get(f"{cfg.alertmanager_url}/-/ready")
                    checks["alertmanager"] = {
                        "ok": response.status_code == 200,
                        "detail": f"HTTP {response.status_code}",
                    }
                except httpx.HTTPError as exc:
                    checks["alertmanager"] = {"ok": False, "detail": str(exc)}
        healthy = all(entry["ok"] for entry in checks.values())
        body = {
            "status": "ok" if healthy else "degraded",
            "checks": checks,
            "last_ready_at": last_ready,
            **run_info_dict(),
        }
        if healthy:
            return body
        return JSONResponse(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, content=body)

    @app.get("/metrics", include_in_schema=False)
    async def metrics() -> Any:
        from fastapi.responses import Response

        return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)

    @app.post("/v1/candidates/cleanup")
    async def candidate_cleanup(
        body: CleanupRequest,
        x_control_plane_key: str = Header(default=""),
    ) -> dict[str, Any]:
        await require_key(x_control_plane_key)
        branches = await service.cleanup_candidate_branches(
            body.repos or None,
            apply=body.apply,
        )
        return {"branches": branches, "dry_run": not body.apply}

    @app.get("/v1/sessions/inspect")
    async def sessions_inspect(
        x_control_plane_key: str = Header(default=""),
    ) -> dict[str, Any]:
        await require_key(x_control_plane_key)
        fields = inspect_session_fields(cfg.agent_session_dir)
        return {"sensitive_field_names": sorted(fields), "count": len(fields)}

    @app.post("/v1/alerts/{fingerprint}/policy")
    async def alert_policy(
        fingerprint: str,
        body: AlertPolicyRequest,
        x_control_plane_key: str = Header(default=""),
    ) -> ApprovalDecisionResponse:
        await require_key(x_control_plane_key)
        message = await service.set_alert_policy(fingerprint, body.policy)
        return ApprovalDecisionResponse(accepted=True, message=message)

    @app.post("/v1/alerts/{fingerprint}/run")
    async def alert_run(
        fingerprint: str,
        x_control_plane_key: str = Header(default=""),
    ) -> ApprovalDecisionResponse:
        await require_key(x_control_plane_key)
        message = await service.run_manual(fingerprint)
        return ApprovalDecisionResponse(accepted=True, message=message)

    @app.post("/v1/tasks")
    async def dispatch_task(
        body: TaskRequest,
        x_control_plane_key: str = Header(default=""),
    ) -> TaskDispatchResponse:
        await require_key(x_control_plane_key)
        task_id, message = await service.dispatch_task(body.prompt, body.repo, body.project)
        return TaskDispatchResponse(task_id=task_id, message=message)

    @app.post("/v1/digest")
    async def run_digest(
        x_control_plane_key: str = Header(default=""),
    ) -> ApprovalDecisionResponse:
        await require_key(x_control_plane_key)
        message = await service.run_digest()
        return ApprovalDecisionResponse(accepted=True, message=message)

    @app.post("/v1/scan")
    async def env_scan(
        x_control_plane_key: str = Header(default=""),
    ) -> ApprovalDecisionResponse:
        await require_key(x_control_plane_key)
        differences = await service.run_env_scan()
        if differences:
            return ApprovalDecisionResponse(accepted=True, message="；\n".join(differences))
        return ApprovalDecisionResponse(accepted=True, message="环境自检通过，无差异")

    @app.post("/v1/candidates/{candidate_id}/dismiss")
    async def dismiss_candidate(
        candidate_id: str,
        body: PromoteRequest,
        x_control_plane_key: str = Header(default=""),
    ) -> ApprovalDecisionResponse:
        await require_key(x_control_plane_key)
        message = service.dismiss_candidate(candidate_id)
        if message.startswith("候选已归档"):
            await notifier.notify(
                "info",
                "候选已归档",
                f"candidate_id={candidate_id}\ndecided_by={body.decided_by or 'unknown'}",
            )
        return ApprovalDecisionResponse(accepted=True, message=message)

    @app.get("/v1/evidence")
    async def evidence_view(x_control_plane_key: str = Header(default="")) -> dict[str, Any]:
        await require_key(x_control_plane_key)
        sessions = []
        session_dir = cfg.data_dir / "agent-sessions"
        if session_dir.is_dir():
            for path in sorted(
                session_dir.glob("*-last.md"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )[:10]:
                sessions.append({"file": str(path), "size": path.stat().st_size, "mtime": int(path.stat().st_mtime)})
        evidence_files = []
        if cfg.evidence_dir.is_dir():
            for path in sorted(
                cfg.evidence_dir.glob("*.json"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )[:10]:
                evidence_files.append(
                    {
                        "file": str(path),
                        "size": path.stat().st_size,
                        "mtime": int(path.stat().st_mtime),
                    }
                )
        return {
            "repairs": [
                {
                    "id": row["id"],
                    "status": row["status"],
                    "attempt": row["attempt"],
                    "result": row["result"],
                }
                for row in store.list_repairs_with_fallback(limit=20)
            ],
            "candidates": [
                {
                    "id": row["id"],
                    "pattern": row["pattern"],
                    "status": row["status"],
                    "times_supported": row["times_supported"],
                }
                for row in store.list_candidates("candidate")
            ],
            "session_summaries": sessions,
            "evidence_files": evidence_files,
        }

    @app.post("/v1/alerts/alertmanager", dependencies=[])
    async def ingest_alerts(
        payload: AlertmanagerPayload,
        x_control_plane_key: str = Header(default=""),
        authorization: str = Header(default=""),
    ) -> AlertResponse:
        if not await valid_alertmanager_key(x_control_plane_key, authorization):
            AUTH_FAILURES.labels(reason="invalid_key", endpoint="/v1/alerts/alertmanager").inc()
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")
        # Canonical Work/Run materialisation happens inside RepairService
        # before the legacy SQLite compatibility projection.  There is no
        # best-effort second write here anymore.
        return await service.ingest(payload)

    @app.post("/v1/approvals/{ref_id}/decision")
    async def approval_decision(
        ref_id: str,
        body: ApprovalDecisionRequest,
        x_control_plane_key: str = Header(default=""),
    ) -> ApprovalDecisionResponse:
        await require_key(x_control_plane_key)
        if body.action not in {"approve", "reject", "rollback"}:
            return ApprovalDecisionResponse(accepted=False, message="Unsupported action")
        store.add_approval(
            f"ap-{uuid.uuid4().hex[:12]}",
            "repair",
            ref_id,
            body.action,
            body.decided_by,
            body.note,
        )
        decided = await approvals.decide(ref_id, body.action)
        if not decided:
            return ApprovalDecisionResponse(accepted=False, message="No pending decision")
        return ApprovalDecisionResponse(accepted=True, message=f"{body.action} recorded")

    @app.post("/v1/candidates/{candidate_id}/promote")
    async def promote_candidate(
        candidate_id: str,
        body: PromoteRequest,
        x_control_plane_key: str = Header(default=""),
    ) -> ApprovalDecisionResponse:
        await require_key(x_control_plane_key)
        candidate = store.get_candidate(candidate_id)
        if candidate is None:
            return ApprovalDecisionResponse(accepted=False, message="Candidate not found")
        if candidate["status"] != "candidate":
            return ApprovalDecisionResponse(accepted=False, message="Candidate is not pending")
        if not candidate["verifier_ids"]:
            return ApprovalDecisionResponse(accepted=False, message="Candidate has no verifier")
        source_repair_id = str(candidate["source_repair_id"] or "")
        source_repair = store.get_repair(source_repair_id) if source_repair_id else None
        if source_repair is None or source_repair["status"] != "closed":
            return ApprovalDecisionResponse(
                accepted=False,
                message="Candidate source repair is not closed and verified",
            )
        # Mirror the human approval into the portable canonical records.  The
        # legacy candidate table remains a compatibility projection. The
        # portable knowledge projection becomes official first; the legacy
        # playbook row is updated only after that canonical transition succeeds.
        if _PORTABLE_AVAILABLE and portable_runtime is not None:
            from portable_runtime.records.authorization import (
                CanonicalAuthorizationRequest,
                create_authorization_use,
                is_authorized_for,
                record_human_approval,
                validate_grant,
            )
            from portable_runtime.records.knowledge import KnowledgeProjection, promote_to_official
            from portable_runtime.records.models import Assertion, ChangeObjectRecord, Derivation
            from portable_runtime.records.relations import RecordRelation

            source_work = portable_runtime.store.get_work(f"work_legacy_{source_repair_id}")
            source_metadata = getattr(source_work, "metadata", {}) if source_work is not None else {}
            if (
                source_work is None
                or source_work.status != "completed"
                or source_metadata.get("verification_status") != "passed"
            ):
                return ApprovalDecisionResponse(
                    accepted=False,
                    message="Canonical source repair is not completed and verified",
                )
            verification_refs = (
                list(source_metadata.get("verification_refs", []))
            )
            record_getter = getattr(portable_runtime.store, "get_record", None)
            relation_getter = getattr(portable_runtime.store, "get_relation", None)
            verification = None
            evidence = None
            relation = None
            for ref in verification_refs:
                value = None
                if callable(record_getter):
                    value = record_getter(str(ref))
                if value is None and callable(relation_getter):
                    value = relation_getter(str(ref))
                metadata = getattr(value, "metadata", {}) if value is not None else {}
                if getattr(value, "relation_type", None) == "validated-under":
                    relation = value
                elif metadata.get("qualification_kind") == "verification" and metadata.get("result") == "pass":
                    verification = value
                elif metadata.get("qualification_kind") == "evidence":
                    evidence = value
            if verification is None or evidence is None or relation is None:
                return ApprovalDecisionResponse(
                    accepted=False,
                    message="Canonical verification evidence is missing; complete deterministic verification first",
                )
            source_run = portable_runtime.store.get_run(f"run_legacy_{source_repair_id}")
            if source_run is None:
                return ApprovalDecisionResponse(
                    accepted=False,
                    message="Canonical verification Run is missing; refusing promotion",
                )
            expected_versions = list(source_metadata.get("subject_version_refs", []))
            expected_scope = source_metadata.get("verification_scope")
            if not isinstance(expected_scope, dict):
                expected_scope = {"work_kind": source_work.kind, "workflow_id": source_run.workflow_id}
            expected_work_version = source_metadata.get("work_version", source_metadata.get("task_version", 1))
            expected_acceptance_criteria = list(source_work.acceptance_criteria)
            expected_criteria_digest = PortableRuntimeAuthority._verification_criteria_digest(source_work)
            for candidate_proof in (verification, evidence):
                proof_metadata = getattr(candidate_proof, "metadata", {})
                proof_result = proof_metadata.get("verification_result")
                if (
                    not isinstance(proof_result, dict)
                    or proof_result.get("result") != "pass"
                    or proof_result.get("work_id") != source_work.id
                    or proof_result.get("run_id") != source_run.id
                    or proof_result.get("scope") != expected_scope
                    or proof_result.get("subject_version_refs") != expected_versions
                    or proof_result.get("work_version") != expected_work_version
                    or proof_result.get("acceptance_criteria") != expected_acceptance_criteria
                    or proof_result.get("criteria_digest") != expected_criteria_digest
                ):
                    return ApprovalDecisionResponse(
                        accepted=False,
                        message="Canonical verification proof is not bound to Work/Run scope, version, and criteria",
                    )
            if (
                getattr(relation, "subject_ref", None) != evidence.id
                or getattr(relation, "object_ref", None) != verification.id
                or getattr(relation, "scope", {}).get("work_id") != source_work.id
                or getattr(relation, "scope", {}).get("run_id") != source_run.id
            ):
                return ApprovalDecisionResponse(
                    accepted=False,
                    message="Canonical verification relation is not bound to the source Work/Run",
                )
            version_ref = f"candidate:{candidate_id}:{candidate['updated_at']}"
            # Official projections require every scope/version proof to
            # resolve to a typed canonical object.  A candidate version is a
            # ChangeObject (not an EvidenceArtifact), and the human approval
            # is represented by an Assertion (not a core Decision) so the
            # projection fields retain their semantic contracts.
            version_record_id = f"record_candidate_{candidate_id}_scope"
            version_record_getter = getattr(portable_runtime.store, "get_record", None)
            version_record = (
                version_record_getter(version_record_id)
                if callable(version_record_getter)
                else None
            )
            if version_record is None:
                version_record = ChangeObjectRecord(
                    id=version_record_id,
                    source_refs=[source_work.id],
                    object_type="knowledge-candidate",
                    current_version_ref=version_ref,
                    metadata={
                        "qualification_kind": "candidate-version",
                        "subject_version_refs": [version_ref],
                        "candidate_id": candidate_id,
                    },
                )
                save_record = getattr(portable_runtime.store, "save_record", None)
                if not callable(save_record):
                    return ApprovalDecisionResponse(
                        accepted=False,
                        message="Portable candidate version record cannot be persisted",
                    )
                save_record(version_record)
            projection_id = f"knowledge_candidate_{candidate_id}"
            decision, grant = record_human_approval(
                portable_runtime.store,
                # The API key authenticates the configured owner.  ``decided_by``
                # is retained as display/audit metadata and must not widen the
                # canonical authority principal.
                principal_ref=canonical_human_principal(cfg.owner_principal),
                grantee_ref=f"control-plane:candidate:{candidate_id}",
                allowed_capabilities=["knowledge.promote"],
                subject_version_refs=[projection_id, f"{projection_id}:v1"],
                work_id=f"work_legacy_{source_repair_id}",
                resource_scope=[f"candidate:{candidate_id}"],
                ttl_seconds=3600,
            )
            get_authorization = getattr(portable_runtime.store, "get_authorization", None)
            if not callable(get_authorization) or get_authorization(grant.id) is None:
                return ApprovalDecisionResponse(
                    accepted=False,
                    message="Portable authorization record was not persisted",
                )
            promotion_request = CanonicalAuthorizationRequest(
                capability="knowledge.promote",
                actor_ref=f"control-plane:candidate:{candidate_id}",
                resource_ref=f"candidate:{candidate_id}",
                subject_version_refs=[projection_id, f"{projection_id}:v1"],
                effect_class="write-local",
            )
            # Candidate promotion is a fixed local write operation.  Do not
            # let a generic approval helper's unconstrained grant or the
            # candidate metadata choose the capability/resource/effect.  The
            # grant and its typed AuthorizationUse are the durable, at-time
            # proof consumed by the canonical graph validator after the grant
            # eventually expires or is revoked.
            grant = grant.model_copy(
                update={
                    "allowed_capabilities": ["knowledge.promote"],
                    "resource_scope": [f"candidate:{candidate_id}"],
                    "effect_ceiling": "write-local",
                }
            )
            save_authorization = getattr(portable_runtime.store, "save_authorization", None)
            if not callable(save_authorization):
                return ApprovalDecisionResponse(
                    accepted=False,
                    message="Portable authorization store cannot persist fixed promotion grant",
                )
            save_authorization(grant)
            if validate_grant(grant) or not is_authorized_for(promotion_request, grant):
                return ApprovalDecisionResponse(
                    accepted=False,
                    message="Portable promotion grant does not authorize this candidate action",
                )
            try:
                authorization_use = create_authorization_use(grant, promotion_request)
            except ValueError as exc:
                return ApprovalDecisionResponse(
                    accepted=False,
                    message=f"Portable promotion authorization proof rejected: {exc}",
                )
            save_authorization_use = getattr(portable_runtime.store, "save_authorization_use", None)
            if not callable(save_authorization_use):
                return ApprovalDecisionResponse(
                    accepted=False,
                    message="Portable authorization-use store is unavailable",
                )
            save_authorization_use(authorization_use)
            approval_assertion_id = f"record_candidate_{candidate_id}_approval"
            approval_assertion = (
                record_getter(approval_assertion_id)
                if callable(record_getter)
                else None
            )
            if approval_assertion is None:
                approval_assertion = Assertion(
                    id=approval_assertion_id,
                    kind="claim",
                    statement=f"Human owner approved promotion of candidate {candidate_id}",
                    lifecycle_status="current",
                    epistemic_status="supported",
                    source_refs=[decision.id],
                    metadata={
                        "qualification_kind": "human-approval",
                        "decision_ref": decision.id,
                        "authorization_ref": grant.id,
                        "candidate_id": candidate_id,
                    },
                )
                save_record = getattr(portable_runtime.store, "save_record", None)
                if not callable(save_record):
                    return ApprovalDecisionResponse(
                        accepted=False,
                        message="Portable approval assertion cannot be persisted",
                    )
                save_record(approval_assertion)
            # The approval assertion is governance evidence, not an epistemic
            # judgment.  Public Runtime validation requires a separate
            # supported judgment bound to the current verification assertion,
            # plus a Derivation that names the judgment, evidence and scope.
            judgment_id = f"record_candidate_{candidate_id}_judgment"
            judgment = record_getter(judgment_id) if callable(record_getter) else None
            if judgment is None:
                judgment = Assertion(
                    id=judgment_id,
                    kind="claim",
                    statement=f"Deterministic verification supports candidate {candidate_id}",
                    lifecycle_status="current",
                    epistemic_status="supported",
                    source_refs=[verification.id],
                    metadata={
                        "qualification_kind": "epistemic-judgment",
                        "epistemic_role": "judgment",
                        "judgment_for_refs": [verification.id],
                        "candidate_id": candidate_id,
                    },
                )
                save_record = getattr(portable_runtime.store, "save_record", None)
                if not callable(save_record):
                    return ApprovalDecisionResponse(
                        accepted=False,
                        message="Portable epistemic judgment cannot be persisted",
                    )
                save_record(judgment)
            derivation_id = f"record_candidate_{candidate_id}_derivation"
            derivation = record_getter(derivation_id) if callable(record_getter) else None
            if derivation is None:
                derivation = Derivation(
                    id=derivation_id,
                    premise_refs=[judgment.id],
                    evidence_refs=[evidence.id],
                    rule_or_method_refs=["control-plane.deterministic-verifier"],
                    conclusion_ref=verification.id,
                    lifecycle_status="current",
                    metadata={
                        "qualification_kind": "candidate-promotion-derivation",
                        "scope_version_refs": [version_record.id],
                        "candidate_id": candidate_id,
                    },
                )
                save_record = getattr(portable_runtime.store, "save_record", None)
                if not callable(save_record):
                    return ApprovalDecisionResponse(
                        accepted=False,
                        message="Portable verification derivation cannot be persisted",
                    )
                save_record(derivation)
            derivation_relation_id = f"relation_candidate_{candidate_id}_derivation"
            derivation_relation = (
                relation_getter(derivation_relation_id)
                if callable(relation_getter)
                else None
            )
            if derivation_relation is None:
                derivation_relation = RecordRelation(
                    id=derivation_relation_id,
                    relation_type="derived-from",
                    subject_ref=verification.id,
                    object_ref=derivation.id,
                    scope={
                        "work_id": source_work.id,
                        "run_id": source_run.id,
                        "scope_version_ref": version_record.id,
                    },
                    created_by="control-plane.deterministic-verifier",
                    metadata={"qualification_kind": "candidate-promotion-derivation"},
                )
                save_relation = getattr(portable_runtime.store, "save_relation", None)
                if not callable(save_relation):
                    return ApprovalDecisionResponse(
                        accepted=False,
                        message="Portable verification derivation relation cannot be persisted",
                    )
                save_relation(derivation_relation)
            projection_getter = getattr(portable_runtime.store, "get_knowledge_projection", None)
            projection = projection_getter(projection_id) if callable(projection_getter) else None
            if projection is None:
                projection = KnowledgeProjection(
                    id=projection_id,
                    kind="repair-playbook",
                    title=str(candidate["pattern"]),
                    source_work_refs=[f"work_legacy_{source_repair_id}"],
                    current_assertion_refs=[verification.id],
                    evidence_summary_refs=[evidence.id],
                    validity_scope={
                        "pattern": str(candidate["pattern"]),
                        "scope": str(candidate["scope"]),
                    },
                    environment_bindings={"candidate_version": version_ref},
                    reopen_conditions=[str(candidate["reopen_conditions"] or "")],
                    epistemic_judgment_refs=[judgment.id],
                    authorization_refs=[grant.id],
                    scope_version_refs=[version_record.id],
                    lifecycle_status="candidate",
                    metadata={
                        "legacy_candidate_id": candidate_id,
                        "source_repair_id": source_repair_id,
                        "tool_sequence": str(candidate["tool_sequence"]),
                        "actor_ref": f"control-plane:candidate:{candidate_id}",
                        "resource_ref": f"candidate:{candidate_id}",
                        "effect_class": "write-local",
                        "authorization_use_ref": authorization_use.id,
                        "promotion_capability": "knowledge.promote",
                    },
                )
            else:
                # Repeated promotion requests must refresh the fixed action
                # proof rather than trusting stale candidate metadata.
                projection = projection.model_copy(
                    update={
                        "authorization_refs": [grant.id],
                        "metadata": {
                            **dict(projection.metadata),
                            "actor_ref": f"control-plane:candidate:{candidate_id}",
                            "resource_ref": f"candidate:{candidate_id}",
                            "effect_class": "write-local",
                            "authorization_use_ref": authorization_use.id,
                        },
                    }
                )
            try:
                official = promote_to_official(projection)
                portable_runtime.store.save_knowledge_projection(official)
            except ValueError as exc:
                return ApprovalDecisionResponse(
                    accepted=False,
                    message=f"Portable knowledge promotion blocked: {exc}",
                )
        store.add_approval(
            f"ap-{uuid.uuid4().hex[:12]}",
            "candidate",
            candidate_id,
            "promote",
            body.decided_by,
            body.note,
        )
        store.promote_candidate(candidate_id)
        await notifier.notify("info", "候选经验已晋升为 official playbook", f"candidate_id={candidate_id}")
        return ApprovalDecisionResponse(accepted=True, message="promoted")

    @app.post("/v1/control/pause")
    async def pause_control(
        body: ControlRequest,
        x_control_plane_key: str = Header(default=""),
    ) -> ApprovalDecisionResponse:
        await require_key(x_control_plane_key)
        store.set_setting("paused", "1")
        await notifier.notify("warning", "控制平面已暂停", body.reason or "手动暂停")
        return ApprovalDecisionResponse(accepted=True, message="paused")

    @app.post("/v1/control/resume")
    async def resume_control(
        body: ControlRequest,
        x_control_plane_key: str = Header(default=""),
    ) -> ApprovalDecisionResponse:
        await require_key(x_control_plane_key)
        store.set_setting("paused", "0")
        await notifier.notify("info", "控制平面已恢复", body.reason or "手动恢复")
        return ApprovalDecisionResponse(accepted=True, message="resumed")

    @app.get("/status")
    async def status_view(x_control_plane_key: str = Header(default="")) -> dict[str, Any]:
        await require_key(x_control_plane_key)
        now = int(__import__("time").time())
        return {
            "paused": service.paused,
            "budget_remaining": budget.remaining(),
            "candidates": len(store.list_candidates("candidate")),
            "official_playbooks": len(store.list_playbooks()),
            "recent_repairs": [
                {
                    "id": row["id"],
                    "fingerprint": row["fingerprint"],
                    "status": row["status"],
                    "attempt": row["attempt"],
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                    "result": row["result"],
                }
                for row in store.list_repairs_with_fallback(limit=20)
            ],
            "server_time": now,
        }

    return app
