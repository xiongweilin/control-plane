from __future__ import annotations

import asyncio
import logging
import uuid
from contextlib import asynccontextmanager, suppress
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, REGISTRY, generate_latest
from pydantic import BaseModel

from .approvals import ApprovalManager
from .budget import Budget
from .codex_runner import CodexRunner
from .config import ControlPlaneConfig
from .metrics import ControlPlaneCollector
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
from .service import RepairService
from .storage import Store

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
    "修复过程会自动推送：开始修复 → Agent 启动 → 心跳 → 验证 → 完成/失败。"
)


class PromoteRequest(BaseModel):
    decided_by: str
    note: str = ""


def create_app(config: ControlPlaneConfig | None = None) -> FastAPI:
    cfg = config or ControlPlaneConfig.load()
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    cfg.patch_dir.mkdir(parents=True, exist_ok=True)
    cfg.evidence_dir.mkdir(parents=True, exist_ok=True)

    store = Store(cfg.state_db)
    budget = Budget(store, cfg.daily_agent_budget, cfg.max_agent_calls_per_repair)
    approvals = ApprovalManager()
    notifier = Notifier(cfg)
    agent = CodexRunner(cfg)
    service = RepairService(cfg, store, budget, agent, approvals, notifier)
    with suppress(ValueError):
        # skip if already registered (e.g. test app created twice)
        REGISTRY.register(ControlPlaneCollector(store, budget.remaining))

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        expired = store.expire_candidates(int(__import__("time").time()))
        if expired:
            logger.info("expired %s candidates", expired)
        if not store.get_setting("usage_hint_sent"):
            await notifier.notify("info", "控制平面使用提示", USAGE_HINT)
            store.set_setting("usage_hint_sent", "1")
        digest_task = asyncio.create_task(service.digest_loop())
        scan_task = asyncio.create_task(service.scan_loop())
        yield
        scan_task.cancel()
        digest_task.cancel()
        await service.close()
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

    async def require_key(x_control_plane_key: str = Header(default="")) -> None:
        if x_control_plane_key != cfg.api_key:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")

    async def valid_key(header_key: str, query_key: str | None) -> bool:
        return header_key == cfg.api_key or (query_key is not None and query_key == cfg.api_key)

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

    @app.get("/metrics", include_in_schema=False)
    async def metrics() -> Any:
        from fastapi.responses import Response

        return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)

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
        api_key: str | None = Query(default=None),
    ) -> AlertResponse:
        if not await valid_key(x_control_plane_key, api_key):
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
