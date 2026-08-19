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
from .config import ControlPlaneConfig
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
from .runtime import (
    acquire_single_instance,
    bootstrap,
    graceful_shutdown,
    run_info_dict,
    with_run_id,
)
from .service import RepairService
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
    budget = Budget(store, cfg.daily_agent_budget, cfg.max_agent_calls_per_repair)
    approvals = ApprovalManager()
    notifier = Notifier(cfg)
    agent = CodexRunner(cfg)
    agent.attach_store(store)
    service = RepairService(cfg, store, budget, agent, approvals, notifier)
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
        resumed: list[str] = []
        for row in store.list_repairs(limit=1_000):
            if row["status"] in keep_pending:
                resumed.append(row["id"])
                continue
            if row["status"] not in {"closed", "failed", "interrupted", "rolled_back"}:
                store.set_repair_status(
                    row["id"],
                    "interrupted",
                    error="control plane restarted",
                    finished_at=now,
                )
                logger.info("reconciled stale repair %s -> interrupted", row["id"])
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
                cleaned = await service.cleanup_candidate_branches(apply=True)
                if cleaned:
                    logger.info(
                        "auto candidate-branch cleanup removed %s stale branches",
                        sum(1 for entry in cleaned if entry.get("deleted")),
                    )
            except Exception:
                logger.exception("auto candidate-branch cleanup failed")
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
                for row in store.list_repairs(limit=20)
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
                for row in store.list_repairs(limit=20)
            ],
            "server_time": now,
        }

    return app
