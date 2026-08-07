from __future__ import annotations

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
    AlertResponse,
    ApprovalDecisionRequest,
    ApprovalDecisionResponse,
    ControlRequest,
    ErrorDetail,
    ErrorResponse,
)
from .notify import Notifier
from .service import RepairService
from .storage import Store

logger = logging.getLogger(__name__)


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
        yield
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
