from __future__ import annotations

import json
import secrets
from typing import Any

import httpx
from fastapi import FastAPI, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, Field

from portable_runtime.controller import CognitiveController, ControllerDecision, ControllerDecisionKind
from portable_runtime.core.capability_contract import CapabilityEffectRule
from portable_runtime.deployment.local import create_personal_platform_runtime
from portable_runtime.interactions.feishu.provider import FeishuHumanProvider, FeishuNotificationProvider
from portable_runtime.providers.codex.provider import CodexProvider

from .audit import inspect_session_fields
from .codex_boundary import CodexExecutionBoundary
from .config import ControlPlaneConfig
from .game_mode import read_game_mode_state
from .personal_operations import PersonalOperationsProvider


class TaskRequest(BaseModel):
    prompt: str = Field(min_length=1)
    repo: str | None = None


class TaskResponse(BaseModel):
    work_id: str
    controller_id: str
    status: str
    result: dict[str, Any] | None = None


class AlertIngestResponse(BaseModel):
    accepted: int
    suppressed: int
    diagnosed: int
    controllers: list[str]


def _register_profile_effect_rules(runtime: Any) -> None:
    rules = [
        CapabilityEffectRule(
            capability="notify.send",
            impact_class="write-remote",
            authorization_required=False,
            resource_required=False,
            version_required=False,
            blast_radius=1,
            exposure=1,
        ),
        CapabilityEffectRule(capability="git.merge", impact_class="write-local", authorization_required=True, resource_required=True, version_required=True, blast_radius=1, exposure=1),
        CapabilityEffectRule(capability="git.push", impact_class="write-remote", authorization_required=True, resource_required=True, version_required=True, blast_radius=2, exposure=2),
        CapabilityEffectRule(capability="git.rollback", impact_class="write-local", authorization_required=True, resource_required=True, version_required=True, blast_radius=2, exposure=2),
        CapabilityEffectRule(capability="docker.restart", impact_class="write-remote", authorization_required=True, resource_required=True, version_required=False, blast_radius=2, exposure=2),
        CapabilityEffectRule(capability="docker.compose.up", impact_class="write-remote", authorization_required=True, resource_required=True, version_required=False, blast_radius=3, exposure=3),
    ]
    for rule in rules:
        runtime.contract_registry.register_effect_rule(rule)


def create_app(config: ControlPlaneConfig | None = None) -> FastAPI:
    cfg = config or ControlPlaneConfig.load()
    cfg.state_db.parent.mkdir(parents=True, exist_ok=True)
    cfg.artifact_root.mkdir(parents=True, exist_ok=True)
    cfg.agent_session_dir.mkdir(parents=True, exist_ok=True)

    runtime = create_personal_platform_runtime(cfg.state_db, cfg.artifact_root)
    runtime.registry.register(
        CodexProvider(
            model=cfg.model,
            cli=cfg.codex_cli,
            gateway_base_url=cfg.gateway_base_url,
            execution_boundary=CodexExecutionBoundary(cfg),
        )
    )
    runtime.registry.register(FeishuHumanProvider())
    if cfg.notification_enabled:
        runtime.registry.register(FeishuNotificationProvider())
    runtime.registry.register(PersonalOperationsProvider(cfg))
    _register_profile_effect_rules(runtime)
    controller = CognitiveController(runtime)

    app = FastAPI(
        title="Personal Control Plane",
        version="0.2.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.kernel_runtime = runtime
    app.state.controller = controller
    app.state.config = cfg

    def require_key(candidate: str) -> None:
        if not candidate or not secrets.compare_digest(candidate, cfg.api_key):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")

    async def run_cognition(*, title: str, prompt: str, repo: str | None = None) -> TaskResponse:
        if repo and not cfg.repo_allowed(repo):
            raise HTTPException(status_code=400, detail="repo is outside personal allowlist")
        work = runtime.create_work(title=title, description=prompt, kind="personal-cognitive-task")
        state = controller.create(subject_ref=work.id, context_refs=[work.id])
        decision = ControllerDecision(
            controller_ref=state.id,
            state_version=state.version,
            kind=ControllerDecisionKind.INVOKE_CAPABILITY,
            capability="reason.generate",
            instruction=prompt,
            parameters={"repo": repo} if repo else {},
            reason="personal profile requested a read-only cognitive step",
        )
        final_state = await controller.apply(decision)
        result_payload: dict[str, Any] | None = None
        for event in reversed(runtime.store.list_events(final_state.id)):
            if event.type == "ControllerCapabilityResultObserved":
                raw = event.payload.get("result")
                if isinstance(raw, dict):
                    result_payload = raw
                break
        return TaskResponse(
            work_id=work.id,
            controller_id=final_state.id,
            status=str((result_payload or {}).get("status", final_state.status.value)),
            result=result_payload,
        )

    async def notify(work_id: str, text: str) -> None:
        if not cfg.notification_enabled:
            return
        try:
            await runtime.run_capability(
                work_id,
                "notify.send",
                instruction=text[:4000],
                actor_ref=cfg.owner_principal,
            )
        except Exception:
            # Notification is secondary evidence delivery; it never changes
            # Work truth, responsibility state, or authorization.
            return

    @app.exception_handler(HTTPException)
    async def http_error(_: Request, exc: HTTPException) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content={"error": str(exc.detail)})

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/live", include_in_schema=False)
    async def live() -> dict[str, str]:
        return {"status": "ok", "runtime_id": runtime.runtime_id}

    @app.get("/ready", include_in_schema=False)
    async def ready() -> Response | dict[str, Any]:
        checks: dict[str, Any] = {}
        timeout = 5.0
        async with httpx.AsyncClient(timeout=timeout) as client:
            for name, base_url in (("prometheus", cfg.prometheus_url), ("alertmanager", cfg.alertmanager_url)):
                if not base_url:
                    continue
                try:
                    response = await client.get(f"{base_url.rstrip('/')}/-/ready")
                    checks[name] = {"ok": response.status_code == 200, "status": response.status_code}
                except httpx.HTTPError as exc:
                    checks[name] = {"ok": False, "detail": str(exc)}
        body = {"status": "ok" if all(v["ok"] for v in checks.values()) else "degraded", "checks": checks, "kernel": await runtime.health()}
        if body["status"] == "ok":
            return body
        return JSONResponse(status_code=503, content=body)

    @app.get("/metrics", include_in_schema=False)
    async def metrics() -> Response:
        return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)

    @app.get("/v1/runtime")
    async def runtime_view(x_control_plane_key: str = Header(default="")) -> dict[str, Any]:
        require_key(x_control_plane_key)
        return {
            "runtime": await runtime.health(),
            "work": [item.model_dump(mode="json") for item in runtime.list_work()[-50:]],
            "contracts": [item.model_dump(mode="json") for item in runtime.contract_registry.list()],
        }

    @app.post("/v1/tasks", response_model=TaskResponse)
    async def task(body: TaskRequest, x_control_plane_key: str = Header(default="")) -> TaskResponse:
        require_key(x_control_plane_key)
        result = await run_cognition(title="Personal task", prompt=body.prompt, repo=body.repo)
        await notify(result.work_id, f"任务已进入 Agent Kernel：{result.work_id}\nstatus={result.status}")
        return result

    @app.post("/v1/alerts/alertmanager", response_model=AlertIngestResponse)
    async def alertmanager(payload: dict[str, Any], x_control_plane_key: str = Header(default=""), authorization: str = Header(default="")) -> AlertIngestResponse:
        bearer = authorization.partition(" ")
        bearer_value = bearer[2] if len(bearer) == 3 and bearer[0].lower() == "bearer" else ""
        if not ((x_control_plane_key and secrets.compare_digest(x_control_plane_key, cfg.api_key)) or (bearer_value and secrets.compare_digest(bearer_value, cfg.api_key))):
            raise HTTPException(status_code=401, detail="Unauthorized")

        raw_alerts = payload.get("alerts", [])
        alerts = raw_alerts if isinstance(raw_alerts, list) else []
        game = read_game_mode_state(
            cfg.game_mode_state_path,
            active_max_age_seconds=cfg.game_mode_active_max_age_seconds,
            restore_grace_seconds=cfg.game_mode_restore_grace_seconds,
        ) if cfg.game_mode_enabled else None
        accepted = suppressed = diagnosed = 0
        controllers: list[str] = []
        for raw in alerts:
            if not isinstance(raw, dict):
                continue
            accepted += 1
            labels = raw.get("labels") if isinstance(raw.get("labels"), dict) else {}
            alert_status = str(raw.get("status", "firing"))
            alertname = str(labels.get("alertname", "unknown"))
            job = str(labels.get("job", ""))
            if alert_status != "firing":
                continue
            if game is not None and game.suppress_alerts and (alertname in cfg.game_mode_alertnames or job in cfg.game_mode_scrape_jobs):
                suppressed += 1
                continue
            project = str(labels.get("project", ""))
            repo = cfg.project_dirs.get(project) if project else None
            prompt = (
                "Diagnose this current Alertmanager fact. Do not claim recovery, authorization, or objective completion. "
                "Return likely causes, discriminating observations, and the safest next cognitive or operational step.\n"
                + json.dumps(raw, ensure_ascii=False, default=str)
            )
            result = await run_cognition(title=f"Alert: {alertname}", prompt=prompt, repo=repo)
            diagnosed += 1
            controllers.append(result.controller_id)
            await notify(result.work_id, f"告警已进入 Agent Kernel：{alertname}\nwork={result.work_id}\nstatus={result.status}")
        return AlertIngestResponse(accepted=accepted, suppressed=suppressed, diagnosed=diagnosed, controllers=controllers)

    @app.get("/v1/game-mode")
    async def game_mode(x_control_plane_key: str = Header(default="")) -> dict[str, Any]:
        require_key(x_control_plane_key)
        state = read_game_mode_state(
            cfg.game_mode_state_path,
            active_max_age_seconds=cfg.game_mode_active_max_age_seconds,
            restore_grace_seconds=cfg.game_mode_restore_grace_seconds,
        )
        return {
            "phase": state.phase,
            "status": state.status,
            "reason": state.reason,
            "suppress_alerts": state.suppress_alerts,
        }

    @app.get("/v1/sessions/inspect")
    async def sessions(x_control_plane_key: str = Header(default="")) -> dict[str, Any]:
        require_key(x_control_plane_key)
        fields = inspect_session_fields(cfg.agent_session_dir)
        return {"sensitive_field_names": sorted(fields), "count": len(fields)}

    return app
