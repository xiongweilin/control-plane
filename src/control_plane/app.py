from __future__ import annotations

import json
import secrets
from typing import Any, cast

import httpx
from fastapi import FastAPI, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse, Response
from portable_runtime.controller import CognitiveController, ControllerStatus
from portable_runtime.core.capability_contract import CapabilityEffectRule
from portable_runtime.core.models import Event, new_id
from portable_runtime.deployment.local import create_personal_platform_runtime
from portable_runtime.interactions.feishu.provider import (
    FeishuHumanProvider,
    FeishuNotificationProvider,
)
from portable_runtime.providers.codex.provider import CodexProvider
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, generate_latest
from pydantic import BaseModel, Field

from .alert_policy import AutonomousRepairPolicy, ManualTaskPolicy, drive_policy
from .audit import inspect_session_fields, redact_value
from .codex_boundary import CodexExecutionBoundary
from .config import ControlPlaneConfig
from .environment import FAIL_SAFE_ALERT_NAMES, EnvironmentInspectionProvider
from .escalation_policy import preserve_blocked_wait
from .game_mode import read_game_mode_state
from .kernel_bridge import PERSONAL_HUMAN_INSTRUCTION_EVENT, PersonalKernelBridge
from .metrics import ControlPlaneMetricsCollector
from .monitoring import PersonalMonitoringProvider
from .personal_operations import PersonalOperationsProvider


class TaskRequest(BaseModel):
    prompt: str = Field(min_length=1)
    repo: str | None = None
    project: str | None = None


class TaskResponse(BaseModel):
    work_id: str
    controller_id: str
    status: str
    message: str = ""
    result: dict[str, Any] | None = None


class ControllerCommandRequest(BaseModel):
    command: str = Field(min_length=1)


class AlertIngestResponse(BaseModel):
    accepted: int
    suppressed: int
    repaired: int
    waiting: int
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
        CapabilityEffectRule(
            capability="git.merge",
            impact_class="write-local",
            authorization_required=True,
            resource_required=True,
            version_required=True,
            blast_radius=1,
            exposure=1,
        ),
        CapabilityEffectRule(
            capability="git.push",
            impact_class="write-remote",
            authorization_required=True,
            resource_required=True,
            version_required=True,
            blast_radius=2,
            exposure=2,
        ),
        CapabilityEffectRule(
            capability="git.rollback",
            impact_class="write-local",
            authorization_required=True,
            resource_required=True,
            version_required=True,
            blast_radius=2,
            exposure=2,
        ),
        CapabilityEffectRule(
            capability="docker.restart",
            impact_class="write-remote",
            authorization_required=True,
            resource_required=True,
            version_required=False,
            blast_radius=2,
            exposure=2,
        ),
        # allowed_auto_projects is the standing personal authorization for this
        # one bounded apply operation; the provider independently enforces it.
        CapabilityEffectRule(
            capability="docker.compose.up",
            impact_class="write-remote",
            authorization_required=False,
            resource_required=False,
            version_required=False,
            blast_radius=3,
            exposure=3,
        ),
        CapabilityEffectRule(
            capability="maintenance.cleanup_known_garbage",
            impact_class="write-local",
            authorization_required=False,
            resource_required=False,
            version_required=False,
            blast_radius=1,
            exposure=1,
        ),
    ]
    for rule in rules:
        runtime.contract_registry.register_effect_rule(rule)


def _task_response(bridge: PersonalKernelBridge, state: Any) -> TaskResponse:
    result = bridge.latest_result(state.id)
    message = str((result or {}).get("message", ""))[:20_000]
    work = bridge.work_for_state(state)
    return TaskResponse(
        work_id=work.id if work is not None else "",
        controller_id=state.id,
        status=state.status.value,
        message=message,
        result=result,
    )


def _split_controller_reply(prompt: str) -> tuple[str, str] | None:
    """Recognize `<controller_id> <explicit command>` from Feishu `/task`."""
    first, sep, rest = prompt.strip().partition(" ")
    if not sep or not first.startswith("controller_") or not rest.strip():
        return None
    return first, rest.strip()


def _safe_alert_payload(raw: dict[str, Any]) -> dict[str, Any]:
    """Keep durable alert context bounded and free of arbitrary webhook fields."""

    labels = raw.get("labels")
    annotations = raw.get("annotations")
    safe_labels = {
        key: str(labels[key])
        for key in ("alertname", "job", "project", "instance", "severity")
        if isinstance(labels, dict) and key in labels
    }
    safe_annotations = {
        key: str(annotations[key])[:2000]
        for key in ("summary", "description")
        if isinstance(annotations, dict) and key in annotations
    }
    return cast(
        dict[str, Any],
        redact_value(
            {
                "status": str(raw.get("status", "firing")),
                "labels": safe_labels,
                "annotations": safe_annotations,
            }
        ),
    )


def create_app(config: ControlPlaneConfig | None = None) -> FastAPI:
    cfg = config or ControlPlaneConfig.load()
    cfg.state_db.parent.mkdir(parents=True, exist_ok=True)
    cfg.artifact_root.mkdir(parents=True, exist_ok=True)
    cfg.agent_session_dir.mkdir(parents=True, exist_ok=True)

    runtime = create_personal_platform_runtime(cfg.state_db, cfg.artifact_root)
    runtime.registry.register(
        CodexProvider(
            model=cfg.diagnosis_model,
            cli=cfg.codex_cli,
            gateway_base_url=cfg.gateway_base_url,
            execution_boundary=CodexExecutionBoundary(cfg),
        )
    )
    runtime.registry.register(FeishuHumanProvider())
    if cfg.notification_enabled:
        runtime.registry.register(FeishuNotificationProvider())
    runtime.registry.register(PersonalMonitoringProvider(cfg))
    runtime.registry.register(PersonalOperationsProvider(cfg))
    environment_provider = EnvironmentInspectionProvider(cfg)
    runtime.registry.register(environment_provider)
    _register_profile_effect_rules(runtime)

    controller = CognitiveController(runtime)
    bridge = PersonalKernelBridge(
        runtime,
        controller,
        owner_principal=cfg.owner_principal,
    )
    profile_metrics = ControlPlaneMetricsCollector(runtime, bridge.responsibilities)
    profile_registry = CollectorRegistry(auto_describe=False)
    profile_registry.register(profile_metrics)

    app = FastAPI(
        title="Personal Control Plane",
        version="0.5.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.kernel_runtime = runtime
    app.state.controller = controller
    app.state.kernel_bridge = bridge
    app.state.config = cfg
    app.state.profile_metrics = profile_metrics
    app.state.environment_provider = environment_provider

    def require_key(candidate: str) -> None:
        if not candidate or not secrets.compare_digest(candidate, cfg.api_key):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")

    def resolve_repo(repo: str | None, project: str | None) -> tuple[str | None, str | None]:
        resolved_project = project.strip() if project else ""
        resolved_repo = repo.strip() if repo else ""
        if resolved_project:
            if resolved_project not in cfg.project_dirs:
                raise HTTPException(status_code=400, detail="unknown personal project")
            project_repo = cfg.project_dirs[resolved_project]
            if resolved_repo and resolved_repo != project_repo:
                raise HTTPException(status_code=400, detail="repo does not match personal project")
            resolved_repo = project_repo
        if resolved_repo and not cfg.repo_allowed(resolved_repo):
            raise HTTPException(status_code=400, detail="repo is outside personal allowlist")
        return resolved_repo or None, resolved_project or None

    async def notify(work_id: str, text: str) -> None:
        if not cfg.notification_enabled or not work_id:
            return
        try:
            await runtime.run_capability(
                work_id,
                "notify.send",
                instruction=text[:4000],
                actor_ref=cfg.owner_principal,
            )
        except Exception:
            # Notification delivery never changes Work truth or repair closure.
            return

    async def settle_personal_state(state: Any) -> Any:
        return await preserve_blocked_wait(controller, bridge, state)

    async def run_manual_task(body: TaskRequest) -> TaskResponse:
        repo, project = resolve_repo(body.repo, body.project)
        state, _assessment_ref = bridge.begin(
            title="Personal task",
            description=body.prompt,
            kind="personal-command",
            repo=repo,
            project=project,
        )
        policy = ManualTaskPolicy(
            controller=controller,
            bridge=bridge,
            prompt=body.prompt,
            diagnosis_model=cfg.diagnosis_model,
            execution_model=cfg.execution_model,
            repo=repo,
        )
        final_state = await drive_policy(controller, state.id, policy)
        final_state = await settle_personal_state(final_state)
        return _task_response(bridge, final_state)

    async def run_alert_repair(
        *,
        title: str,
        description: str,
        repo: str | None,
        project: str | None,
        verification_labels: dict[str, str],
        maintenance_capability: str | None = None,
        fail_safe: bool = False,
    ) -> TaskResponse:
        state, _assessment_ref = bridge.begin(
            title=title,
            description=description,
            kind="personal-incident-repair",
            repo=repo,
            project=project,
            verification_labels=verification_labels,
        )
        policy = AutonomousRepairPolicy(
            controller=controller,
            bridge=bridge,
            prompt=description,
            diagnosis_model=cfg.diagnosis_model,
            execution_model=cfg.execution_model,
            repo=repo,
            project=project if project in cfg.allowed_auto_projects else None,
            verification_labels=verification_labels,
            max_attempts=2,
            maintenance_capability=maintenance_capability,
            fail_safe=fail_safe,
        )
        final_state = await drive_policy(controller, state.id, policy)
        final_state = await settle_personal_state(final_state)
        return _task_response(bridge, final_state)

    async def continue_waiting_controller(controller_id: str, command: str) -> TaskResponse:
        previous = controller.get(controller_id)
        if previous is None:
            raise HTTPException(status_code=404, detail="controller not found")
        if previous.status is not ControllerStatus.WAITING:
            raise HTTPException(status_code=409, detail="controller is not waiting for human input")

        runtime.store.append_event(
            Event(
                id=new_id("event"),
                type=PERSONAL_HUMAN_INSTRUCTION_EVENT,
                subject_ref=controller_id,
                payload={"command": command, "actor_ref": cfg.owner_principal},
            )
        )
        context = bridge.context(previous)
        if context.kind == "personal-incident-repair":
            policy: AutonomousRepairPolicy | ManualTaskPolicy = AutonomousRepairPolicy(
                controller=controller,
                bridge=bridge,
                prompt=context.description,
                diagnosis_model=cfg.diagnosis_model,
                execution_model=cfg.execution_model,
                repo=context.repo,
                project=(
                    context.project
                    if context.project is not None
                    and context.project in cfg.allowed_auto_projects
                    else None
                ),
                verification_labels=context.verification_labels,
                human_instruction=command,
                max_attempts=2,
            )
        else:
            policy = ManualTaskPolicy(
                controller=controller,
                bridge=bridge,
                prompt=context.description,
                diagnosis_model=cfg.diagnosis_model,
                execution_model=cfg.execution_model,
                repo=context.repo,
                human_instruction=command,
            )

        final_state = await drive_policy(controller, previous.id, policy)
        final_state = await settle_personal_state(final_state)
        result = _task_response(bridge, final_state)
        if (
            context.kind == "personal-incident-repair"
            and result.status == ControllerStatus.WAITING.value
        ):
            await notify(
                result.work_id,
                "收到明确命令后仍未解决, 已再次停止.\n"
                f"work={result.work_id}\ncontroller={result.controller_id}\n"
                f"继续命令: /task {result.controller_id} <命令>",
            )
        return result

    @app.exception_handler(HTTPException)
    async def http_error(_: Request, exc: HTTPException) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content={"error": str(exc.detail)})

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/live", include_in_schema=False)
    async def live() -> dict[str, str]:
        return {"status": "ok", "runtime_id": runtime.runtime_id}

    @app.get("/ready", include_in_schema=False, response_model=None)
    async def ready() -> Response | dict[str, Any]:
        checks: dict[str, Any] = {}
        timeout = 5.0
        async with httpx.AsyncClient(timeout=timeout) as client:
            for name, base_url in (
                ("prometheus", cfg.prometheus_url),
                ("alertmanager", cfg.alertmanager_url),
            ):
                if not base_url:
                    continue
                try:
                    response = await client.get(f"{base_url.rstrip('/')}/-/ready")
                    checks[name] = {
                        "ok": response.status_code == 200,
                        "status": response.status_code,
                    }
                except httpx.HTTPError as exc:
                    checks[name] = {"ok": False, "detail": str(exc)}
        kernel = await runtime.health()
        provider_health = {
            str(item.get("provider_id")): item
            for item in kernel.get("providers", [])
            if isinstance(item, dict) and item.get("provider_id")
        }
        environment_provider.set_provider_health(provider_health)
        unavailable_providers = [
            str(provider.get("provider_id", "unknown"))
            for provider in kernel.get("providers", [])
            if isinstance(provider, dict) and provider.get("available") is False
        ]
        ready_ok = all(value["ok"] for value in checks.values()) and not unavailable_providers
        profile_metrics.record_readiness(ready_ok, kernel)
        body = {
            "status": (
                "ok"
                if ready_ok
                else "degraded"
            ),
            "checks": checks,
            "kernel": kernel,
        }
        if unavailable_providers:
            body["unavailable_providers"] = unavailable_providers
        if body["status"] == "ok":
            return body
        return JSONResponse(status_code=503, content=body)

    @app.get("/metrics", include_in_schema=False)
    async def metrics() -> Response:
        from portable_runtime.core import metrics as runtime_metrics

        kernel = await runtime.health()
        environment_provider.set_provider_health(
            {
                str(item.get("provider_id")): item
                for item in kernel.get("providers", [])
                if isinstance(item, dict) and item.get("provider_id")
            }
        )
        profile_metrics.record_current_provider_health(kernel)
        if cfg.environment_enabled:
            profile_metrics.set_environment_snapshot(await environment_provider.refresh())

        # Keep Agent Kernel's custom registry visible through the profile
        # endpoint and refresh its current-state gauges on every scrape.
        works = runtime.store.list_work()
        work_counts: dict[str, int] = {}
        for work in works:
            work_counts[work.status] = work_counts.get(work.status, 0) + 1
        runtime_metrics.snapshot_work_status(work_counts)
        runs = runtime.store.list_runs()
        run_counts: dict[str, int] = {}
        for run in runs:
            run_counts[run.status] = run_counts.get(run.status, 0) + 1
        runtime_metrics.snapshot_run_status(run_counts)

        content = b"".join(
            (
                generate_latest(),
                generate_latest(profile_registry),
                runtime_metrics.generate_metrics_content()[0],
            )
        )
        return Response(content=content, media_type=CONTENT_TYPE_LATEST)

    @app.get("/status")
    async def status_view(x_control_plane_key: str = Header(default="")) -> dict[str, Any]:
        require_key(x_control_plane_key)
        return {
            "status": "ok",
            "runtime": await runtime.health(),
            "diagnosis_model": cfg.diagnosis_model,
            "execution_model": cfg.execution_model,
        }

    @app.get("/v1/runtime")
    async def runtime_view(x_control_plane_key: str = Header(default="")) -> dict[str, Any]:
        require_key(x_control_plane_key)
        return {
            "runtime": await runtime.health(),
            "work": [item.model_dump(mode="json") for item in runtime.list_work()[-50:]],
            "contracts": [
                item.model_dump(mode="json") for item in runtime.contract_registry.list()
            ],
        }

    @app.post("/v1/tasks", response_model=TaskResponse)
    async def task(
        body: TaskRequest,
        x_control_plane_key: str = Header(default=""),
    ) -> TaskResponse:
        require_key(x_control_plane_key)
        controller_reply = _split_controller_reply(body.prompt)
        if controller_reply is not None:
            controller_id, command = controller_reply
            return await continue_waiting_controller(controller_id, command)
        return await run_manual_task(body)

    @app.post("/v1/controllers/{controller_id}/command", response_model=TaskResponse)
    async def controller_command(
        controller_id: str,
        body: ControllerCommandRequest,
        x_control_plane_key: str = Header(default=""),
    ) -> TaskResponse:
        require_key(x_control_plane_key)
        return await continue_waiting_controller(controller_id, body.command)

    @app.post("/v1/alerts/alertmanager", response_model=AlertIngestResponse)
    async def alertmanager(
        payload: dict[str, Any],
        x_control_plane_key: str = Header(default=""),
        authorization: str = Header(default=""),
    ) -> AlertIngestResponse:
        bearer = authorization.partition(" ")
        bearer_value = bearer[2] if len(bearer) == 3 and bearer[0].lower() == "bearer" else ""
        if not (
            (
                x_control_plane_key
                and secrets.compare_digest(x_control_plane_key, cfg.api_key)
            )
            or (bearer_value and secrets.compare_digest(bearer_value, cfg.api_key))
        ):
            raise HTTPException(status_code=401, detail="Unauthorized")

        raw_alerts = payload.get("alerts", [])
        alerts = raw_alerts if isinstance(raw_alerts, list) else []
        game = (
            read_game_mode_state(
                cfg.game_mode_state_path,
                active_max_age_seconds=cfg.game_mode_active_max_age_seconds,
                restore_grace_seconds=cfg.game_mode_restore_grace_seconds,
            )
            if cfg.game_mode_enabled
            else None
        )
        accepted = suppressed = repaired = waiting = 0
        controllers: list[str] = []
        for raw in alerts:
            if not isinstance(raw, dict):
                continue
            accepted += 1
            raw_labels = raw.get("labels")
            labels = raw_labels if isinstance(raw_labels, dict) else {}
            alert_status = str(raw.get("status", "firing"))
            alertname = str(labels.get("alertname", "unknown"))
            job = str(labels.get("job", ""))
            if alert_status != "firing":
                continue
            if game is not None and game.suppress_alerts and (
                alertname in cfg.game_mode_alertnames or job in cfg.game_mode_scrape_jobs
            ):
                suppressed += 1
                continue
            project = str(labels.get("project", "")) or None
            repo = cfg.project_dirs.get(project) if project else None
            verification_labels = {
                key: str(labels.get(key, ""))
                for key in ("alertname", "job", "project", "instance")
                if str(labels.get(key, ""))
            }
            description = json.dumps(
                _safe_alert_payload(raw),
                ensure_ascii=False,
                default=str,
            )[:12_000]
            automatic_maintenance = (
                cfg.automatic_handling_enabled
                and alertname in cfg.auto_maintenance_alertnames
            )
            fail_safe = alertname in FAIL_SAFE_ALERT_NAMES
            if fail_safe:
                result = await run_alert_repair(
                    title=f"Alert (fail-safe): {alertname}",
                    description=(
                        "This alert is fail-safe only. Invoke Codex for diagnosis, but do not "
                        "execute any effect without explicit owner authorization.\n"
                        f"{description}"
                    ),
                    repo=None,
                    project=None,
                    verification_labels=verification_labels,
                    fail_safe=True,
                )
            elif alertname not in cfg.auto_repair_alertnames and not automatic_maintenance:
                result = await run_alert_repair(
                    title=f"Alert (blocked): {alertname}",
                    description=(
                        "This alert is not allowlisted for effects. Invoke Codex for diagnosis, "
                        "but do not execute any effect without explicit owner authorization.\n"
                        f"{description}"
                    ),
                    repo=None,
                    project=None,
                    verification_labels=verification_labels,
                    fail_safe=True,
                )
            else:
                result = await run_alert_repair(
                    title=f"Alert: {alertname}",
                    description=description,
                    repo=repo,
                    project=project,
                    verification_labels=verification_labels,
                    maintenance_capability=(
                        "maintenance.cleanup_known_garbage"
                        if automatic_maintenance
                        else None
                    ),
                )
            controllers.append(result.controller_id)
            if result.status == ControllerStatus.CLOSED.value:
                repaired += 1
            else:
                waiting += 1
                if (
                    alertname in FAIL_SAFE_ALERT_NAMES
                    or (
                        alertname not in cfg.auto_repair_alertnames
                        and not automatic_maintenance
                    )
                ):
                    message = (
                        "环境告警已按 fail-safe 处理, 未执行任何修复 effect, 等待人工升级。\n"
                    )
                else:
                    message = "自动修复两次仍未解决, 已停止并等待明确命令。\n"
                await notify(
                    result.work_id,
                    message
                    + f"alert={alertname}\nwork={result.work_id}\n"
                    f"controller={result.controller_id}\n"
                    f"回复: /task {result.controller_id} <明确命令>",
                )
        return AlertIngestResponse(
            accepted=accepted,
            suppressed=suppressed,
            repaired=repaired,
            waiting=waiting,
            controllers=controllers,
        )

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
