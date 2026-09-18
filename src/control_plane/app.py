from __future__ import annotations

import asyncio
import secrets
import sqlite3
import time
from contextlib import suppress
from pathlib import Path
from typing import Any, cast

import httpx
from agent_kernel.controller import CognitiveController, ControllerStatus
from agent_kernel.core.capabilities import CapabilityRequest
from agent_kernel.core.models import Event, new_id
from agent_kernel.deployment.local import create_personal_platform_runtime
from agent_kernel.providers.codex.provider import CodexProvider
from fastapi import FastAPI, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, generate_latest
from pydantic import BaseModel, Field

from .alert_context import (
    AlertContext,
    alert_context_from_event_payload,
    render_alert_context,
    sanitize_alert_context,
)
from .alert_diagnostics import (
    AlertRepair,
    _alert_diagnosis_snapshot,
    _alert_escalation_reason,
    _alert_fingerprint,
    _format_alert_escalation,
)
from .alert_policy import (
    AutonomousRepairPolicy,
    ManualTaskPolicy,
    drive_policy,
)
from .audit import inspect_session_fields, redact_value
from .codex_boundary import CodexExecutionBoundary, ThreadIsolatedCodexProvider
from .config import ControlPlaneConfig
from .environment import (
    HISTORICAL_SAFETY_HINT_ALERT_NAMES,
    EnvironmentInspectionProvider,
)
from .game_mode import read_game_mode_state
from .kernel_bridge import PERSONAL_HUMAN_INSTRUCTION_EVENT, PersonalKernelBridge
from .metrics import ControlPlaneMetricsCollector
from .monitoring import PersonalMonitoringProvider
from .personal_operations import PersonalOperationsProvider
from .profile_effect_rules import register_profile_effect_rules
from .providers import FeishuHumanProvider, FeishuNotificationProvider
from .state_reconciliation import (
    reconcile_repair_state,
    settle_waiting_execution_claims,
)


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
    queued: int = 0
    deduplicated: int = 0


ALERT_QUEUED_EVENT = "ControlPlaneAlertQueued"
ALERT_FINISHED_EVENT = "ControlPlaneAlertFinished"
ALERT_RESOLVED_EVENT = "ControlPlaneAlertResolved"
ALERT_ESCALATED_EVENT = "ControlPlaneAlertEscalated"
_SYNC_ALERT_NAME = "ControlPlaneSynchronizationDegraded"
_LINE_ENDING_DISCARD_CAPABILITY = "git.discard_line_ending_changes"


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


def create_app(config: ControlPlaneConfig | None = None) -> FastAPI:
    cfg = config or ControlPlaneConfig.load()
    cfg.state_db.parent.mkdir(parents=True, exist_ok=True)
    cfg.artifact_root.mkdir(parents=True, exist_ok=True)
    cfg.agent_session_dir.mkdir(parents=True, exist_ok=True)

    runtime = create_personal_platform_runtime(cfg.state_db, cfg.artifact_root)
    reconcile_repair_state(runtime, stale_after_seconds=900)
    runtime.registry.register(
        ThreadIsolatedCodexProvider(
            CodexProvider(
                model=cfg.diagnosis_model,
                cli=cfg.codex_cli,
                gateway_base_url=cfg.gateway_base_url,
                execution_boundary=CodexExecutionBoundary(cfg),
            )
        )
    )
    runtime.registry.register(FeishuHumanProvider())
    if cfg.notification_enabled:
        runtime.registry.register(FeishuNotificationProvider())
    runtime.registry.register(PersonalMonitoringProvider(cfg))
    runtime.registry.register(PersonalOperationsProvider(cfg))
    environment_provider = EnvironmentInspectionProvider(cfg)
    runtime.registry.register(environment_provider)
    register_profile_effect_rules(runtime)

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

    alert_tasks: dict[str, asyncio.Task[None]] = {}
    active_alerts: dict[str, str] = {}
    alert_lock = asyncio.Lock()
    repair_slots = asyncio.Semaphore(max(1, cfg.max_concurrent))
    recovery_task: asyncio.Task[None] | None = None
    recovery_complete = asyncio.Event()
    recovery_complete.set()
    environment_refresh_task: asyncio.Task[None] | None = None
    metrics_refresh_task: asyncio.Task[None] | None = None
    metrics_content_cache: tuple[float, bytes] | None = None
    health_lock = asyncio.Lock()
    health_cache: tuple[float, dict[str, Any]] | None = None

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

    async def notify(
        work_id: str,
        text: str,
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        base = {
            "attempted": bool(cfg.notification_enabled),
            "provider_accepted": False,
            "provider_status": "disabled",
            "delivery_confirmed": False,
            "delivery_confirmation": "not_available",
            "failure_phase": None,
            "error": None,
        }
        if not cfg.notification_enabled:
            return base
        try:
            if work_id:
                result = await runtime.run_capability(
                    work_id,
                    "notify.send",
                    instruction=text[:12_000],
                    actor_ref=cfg.owner_principal,
                    idempotency_key=idempotency_key,
                )
            else:
                result = await runtime.invoke(
                    CapabilityRequest(
                        id=new_id("request"),
                        capability="notify.send",
                        instruction=text[:12_000],
                        actor_ref=cfg.owner_principal,
                        idempotency_key=idempotency_key,
                        effect_class="write-remote",
                        metadata={"notification_subject": "alert-escalation"},
                    )
                )
        except Exception as exc:
            return {
                **base,
                "provider_status": "provider_error",
                "failure_phase": "control_plane",
                "error": {"type": type(exc).__name__, "message": str(exc)[:500]},
            }
        provider_status = str(getattr(result, "status", "unknown"))
        metadata = getattr(result, "metadata", {})
        metadata = metadata if isinstance(metadata, dict) else {}
        provider_accepted = metadata.get("provider_accepted")
        if not isinstance(provider_accepted, bool):
            provider_accepted = None if provider_status == "succeeded" else False
        return {
            "attempted": True,
            "provider_accepted": provider_accepted,
            "provider_status": provider_status,
            "delivery_confirmed": metadata.get("delivery_confirmed") is True,
            "delivery_confirmation": str(
                metadata.get("delivery_confirmation") or "not_available"
            ),
            "failure_phase": metadata.get("failure_phase"),
            "error": getattr(result, "error", None),
        }

    async def settle_personal_state(state: Any) -> Any:
        # A failed or invalid diagnosis is durable controller state, not a
        # valid diagnosis. Never manufacture a CognitiveClosure or blocked
        # Work merely to give that failure an id.
        if state.status is ControllerStatus.WAITING:
            settle_waiting_execution_claims(runtime, bridge.work_for_state(state))
        return state

    async def kernel_health() -> dict[str, Any]:
        nonlocal health_cache
        now = time.monotonic()
        if health_cache is not None and now - health_cache[0] < 5:
            return health_cache[1]
        async with health_lock:
            now = time.monotonic()
            if health_cache is not None and now - health_cache[0] < 5:
                return health_cache[1]
            try:
                value = cast(
                    dict[str, Any], await asyncio.wait_for(runtime.health(), timeout=5)
                )
            except Exception as exc:
                value = {
                    "runtime_id": runtime.runtime_id,
                    "providers": [
                        {
                            "provider_id": "kernel-health",
                            "available": False,
                            "detail": f"health probe failed: {type(exc).__name__}",
                        }
                    ],
                }
            health_cache = (time.monotonic(), value)
            return value

    async def refresh_environment_background() -> None:
        try:
            snapshot = await environment_provider.refresh()
            profile_metrics.set_environment_snapshot(snapshot)
        except Exception:
            # The next scheduled scrape retries the read-only probe. A probe
            # failure must never affect HTTP liveness or alert ingestion.
            return

    def schedule_environment_refresh() -> None:
        nonlocal environment_refresh_task
        if not cfg.environment_enabled:
            return
        if environment_refresh_task is not None and not environment_refresh_task.done():
            return
        environment_refresh_task = asyncio.create_task(refresh_environment_background())

        def consume_environment_result(done: asyncio.Task[None]) -> None:
            with suppress(asyncio.CancelledError):
                done.exception()

        environment_refresh_task.add_done_callback(consume_environment_result)

    async def refresh_metrics_background() -> None:
        nonlocal metrics_content_cache
        from agent_kernel.core import metrics as runtime_metrics

        kernel = await kernel_health()
        provider_health = {
            str(item.get("provider_id")): item
            for item in kernel.get("providers", [])
            if isinstance(item, dict) and item.get("provider_id")
        }
        environment_provider.set_provider_health(provider_health)
        profile_metrics.record_current_provider_health(kernel)
        if cfg.environment_enabled:
            snapshot = environment_provider.snapshot
            profile_metrics.set_environment_snapshot(snapshot)
            if (
                snapshot is None
                or time.time() - snapshot.checked_at >= cfg.environment_cache_seconds
            ):
                schedule_environment_refresh()

        def collect_metrics() -> bytes:
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
            return b"".join(
                (
                    generate_latest(),
                    generate_latest(profile_registry),
                    runtime_metrics.generate_metrics_content()[0],
                )
            )

        metrics_content_cache = (time.monotonic(), await asyncio.to_thread(collect_metrics))

    def schedule_metrics_refresh() -> None:
        nonlocal metrics_refresh_task
        if metrics_refresh_task is not None and not metrics_refresh_task.done():
            return
        metrics_refresh_task = asyncio.create_task(refresh_metrics_background())

        def consume_metrics_result(done: asyncio.Task[None]) -> None:
            with suppress(asyncio.CancelledError):
                done.exception()

        metrics_refresh_task.add_done_callback(consume_metrics_result)

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

    def begin_alert_repair(
        *,
        fingerprint: str,
        title: str,
        description: str,
        repo: str | None,
        project: str | None,
        verification_labels: dict[str, str],
        maintenance_capability: str | None = None,
        maintenance_parameters: dict[str, str] | None = None,
        alert_context: AlertContext | None = None,
    ) -> AlertRepair:
        state, _assessment_ref = bridge.begin(
            title=title,
            description=description,
            kind="personal-incident-repair",
            repo=repo,
            project=project,
            verification_labels=verification_labels,
        )
        return AlertRepair(
            fingerprint=fingerprint,
            controller_id=state.id,
            title=title,
            description=description,
            repo=repo,
            project=project,
            verification_labels=verification_labels,
            maintenance_capability=maintenance_capability,
            maintenance_parameters=dict(maintenance_parameters or {}),
            alert_context=alert_context or AlertContext(),
        )

    def alert_policy(spec: AlertRepair) -> AutonomousRepairPolicy:
        return AutonomousRepairPolicy(
            controller=controller,
            bridge=bridge,
            prompt=spec.description,
            diagnosis_model=cfg.diagnosis_model,
            execution_model=cfg.execution_model,
            repo=spec.repo,
            project=spec.project if spec.project in cfg.allowed_auto_projects else None,
            verification_labels=spec.verification_labels,
            maintenance_capability=spec.maintenance_capability,
            maintenance_parameters=spec.maintenance_parameters,
        )

    def alert_event_payload(spec: AlertRepair) -> dict[str, Any]:
        return {
            "fingerprint": spec.fingerprint,
            "controller_id": spec.controller_id,
            "title": spec.title[:500],
            "description": spec.description[:12_000],
            "repo": spec.repo,
            "project": spec.project,
            "verification_labels": dict(spec.verification_labels),
            "maintenance_capability": spec.maintenance_capability,
            "maintenance_parameters": dict(spec.maintenance_parameters),
            "alert_context": spec.alert_context.to_payload(),
        }

    def append_alert_event(event_type: str, spec: AlertRepair, **extra: Any) -> None:
        payload = {**alert_event_payload(spec), **extra}
        runtime.store.append_event(
            Event(
                id=new_id("event"),
                type=event_type,
                subject_ref=spec.fingerprint,
                payload=payload,
            )
        )

    def escalation_already_sent(fingerprint: str) -> bool:
        events = runtime.store.list_events(fingerprint)
        resolved = [event for event in events if event.type == ALERT_RESOLVED_EVENT]
        last_resolved = max(resolved, key=lambda event: event.created_at) if resolved else None
        return any(
            event.type == ALERT_ESCALATED_EVENT
            and (last_resolved is None or event.created_at > last_resolved.created_at)
            and (
                event.payload.get("notification_provider_accepted") is True
                or event.payload.get("notification_sent") is True
            )
            for event in events
        )

    async def process_alert_repair(spec: AlertRepair) -> None:
        async with repair_slots:
            policy = alert_policy(spec)
            try:
                final_state = await drive_policy(controller, spec.controller_id, policy)
                final_state = await settle_personal_state(final_state)
                result = _task_response(bridge, final_state)
                snapshot = _alert_diagnosis_snapshot(policy, final_state, spec)
                diagnosis_attempts = int(snapshot["diagnosis_attempts"])
                execution_attempts = int(snapshot["execution_attempts"])
                blocker = snapshot["blocker"]
                should_escalate = result.status == ControllerStatus.WAITING.value and (
                    blocker is not None
                    or snapshot["diagnosis_status"] != "valid"
                    or max(diagnosis_attempts, execution_attempts)
                    >= policy.attempt_limit
                )
                append_alert_event(
                    ALERT_FINISHED_EVENT,
                    spec,
                    status=result.status,
                    work_id=result.work_id,
                    diagnosis_status=snapshot["diagnosis_status"],
                    safety_class=snapshot["safety_class"],
                    diagnosis_attempts=diagnosis_attempts,
                    diagnosis_requested_count=snapshot["diagnosis_requested_count"],
                    diagnosis_completed_count=snapshot["diagnosis_completed_count"],
                    diagnosis_valid_count=snapshot["diagnosis_valid_count"],
                    execution_attempts=execution_attempts,
                    blocker=blocker,
                    proposed_action=snapshot["proposed_action"],
                    rollback=snapshot["rollback"],
                    finished_at=time.time(),
                )
                if should_escalate and not escalation_already_sent(spec.fingerprint):
                    notification = await notify(
                        result.work_id,
                        _format_alert_escalation(
                            spec,
                            work_id=result.work_id,
                            controller_id=result.controller_id,
                            controller_status=result.status,
                            snapshot=snapshot,
                        ),
                        idempotency_key=f"alert-escalation:{spec.fingerprint}",
                    )
                    append_alert_event(
                        ALERT_ESCALATED_EVENT,
                        spec,
                        work_id=result.work_id,
                        reason=_alert_escalation_reason(snapshot, "attempt-budget-exhausted"),
                        diagnosis_status=snapshot["diagnosis_status"],
                        safety_class=snapshot["safety_class"],
                        diagnosis_attempts=diagnosis_attempts,
                        diagnosis_requested_count=snapshot["diagnosis_requested_count"],
                        diagnosis_completed_count=snapshot["diagnosis_completed_count"],
                        diagnosis_valid_count=snapshot["diagnosis_valid_count"],
                        execution_attempts=execution_attempts,
                        blocker=blocker,
                        proposed_action=snapshot["proposed_action"],
                        rollback=snapshot["rollback"],
                        notification_attempted=notification["attempted"],
                        notification_provider_accepted=notification["provider_accepted"],
                        notification_provider_status=notification["provider_status"],
                        notification_delivery_confirmed=notification["delivery_confirmed"],
                        notification_delivery_confirmation=notification[
                            "delivery_confirmation"
                        ],
                        notification_failure_phase=notification["failure_phase"],
                        notification_error=notification["error"],
                        escalated_at=time.time(),
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                current = controller.get(spec.controller_id)
                if current is not None:
                    with suppress(Exception):
                        current = await settle_personal_state(current)
                failed_result = _task_response(bridge, current) if current is not None else None
                snapshot = _alert_diagnosis_snapshot(policy, current, spec, error=exc)
                diagnosis_attempts = int(snapshot["diagnosis_attempts"])
                execution_attempts = int(snapshot["execution_attempts"])
                append_alert_event(
                    ALERT_FINISHED_EVENT,
                    spec,
                    status="failed",
                    work_id=failed_result.work_id if failed_result else "",
                    diagnosis_status=snapshot["diagnosis_status"],
                    safety_class=snapshot["safety_class"],
                    diagnosis_attempts=diagnosis_attempts,
                    diagnosis_requested_count=snapshot["diagnosis_requested_count"],
                    diagnosis_completed_count=snapshot["diagnosis_completed_count"],
                    diagnosis_valid_count=snapshot["diagnosis_valid_count"],
                    execution_attempts=execution_attempts,
                    blocker=snapshot["blocker"],
                    proposed_action=snapshot["proposed_action"],
                    rollback=snapshot["rollback"],
                    error=redact_value(f"{type(exc).__name__}: {str(exc)[:500]}"),
                    finished_at=time.time(),
                )
                if (
                    failed_result is not None
                    and (
                        max(diagnosis_attempts, execution_attempts) >= policy.attempt_limit
                        or snapshot["diagnosis_status"] != "valid"
                    )
                    and not escalation_already_sent(spec.fingerprint)
                ):
                    notification = await notify(
                        failed_result.work_id,
                        _format_alert_escalation(
                            spec,
                            work_id=failed_result.work_id,
                            controller_id=spec.controller_id,
                            controller_status=failed_result.status,
                            snapshot=snapshot,
                        ),
                        idempotency_key=f"alert-escalation:{spec.fingerprint}",
                    )
                    append_alert_event(
                        ALERT_ESCALATED_EVENT,
                        spec,
                        work_id=failed_result.work_id,
                        reason=_alert_escalation_reason(snapshot, "policy-exception"),
                        diagnosis_status=snapshot["diagnosis_status"],
                        safety_class=snapshot["safety_class"],
                        diagnosis_attempts=diagnosis_attempts,
                        diagnosis_requested_count=snapshot["diagnosis_requested_count"],
                        diagnosis_completed_count=snapshot["diagnosis_completed_count"],
                        diagnosis_valid_count=snapshot["diagnosis_valid_count"],
                        execution_attempts=execution_attempts,
                        blocker=snapshot["blocker"],
                        proposed_action=snapshot["proposed_action"],
                        rollback=snapshot["rollback"],
                        notification_attempted=notification["attempted"],
                        notification_provider_accepted=notification["provider_accepted"],
                        notification_provider_status=notification["provider_status"],
                        notification_delivery_confirmed=notification["delivery_confirmed"],
                        notification_delivery_confirmation=notification[
                            "delivery_confirmation"
                        ],
                        notification_failure_phase=notification["failure_phase"],
                        notification_error=notification["error"],
                        escalated_at=time.time(),
                    )

    def recoverable_alert_specs() -> tuple[dict[str, str], list[AlertRepair]]:
        latest: dict[str, Event] = {}
        queued: dict[str, AlertRepair] = {}
        journal_types = {ALERT_QUEUED_EVENT, ALERT_FINISHED_EVENT, ALERT_RESOLVED_EVENT}
        store_path = getattr(runtime.store, "path", None)
        if isinstance(store_path, Path) and store_path.is_file():
            with sqlite3.connect(
                f"file:{store_path.resolve()}?mode=ro", uri=True, timeout=1
            ) as connection:
                rows = connection.execute(
                    "SELECT data FROM runtime_records "
                    "WHERE kind='event' AND json_extract(data, '$.type') IN (?, ?, ?) "
                    "ORDER BY created_at ASC, id ASC",
                    tuple(journal_types),
                ).fetchall()
            journal_events = [Event.model_validate_json(row[0]) for row in rows]
        else:
            journal_events = [
                event
                for event in runtime.store.list_events()
                if event.type in journal_types
            ]
        for event in journal_events:
            if event.type not in {ALERT_QUEUED_EVENT, ALERT_FINISHED_EVENT, ALERT_RESOLVED_EVENT}:
                continue
            fingerprint = str(event.payload.get("fingerprint") or event.subject_ref)
            latest[fingerprint] = event
            if event.type == ALERT_QUEUED_EVENT:
                alert_context = alert_context_from_event_payload(event.payload)
                queued[fingerprint] = AlertRepair(
                    fingerprint=fingerprint,
                    controller_id=str(event.payload.get("controller_id", "")),
                    title=str(event.payload.get("title", "Alert")),
                    description=str(event.payload.get("description", "")),
                    repo=str(event.payload["repo"]) if event.payload.get("repo") else None,
                    project=(
                        str(event.payload["project"]) if event.payload.get("project") else None
                    ),
                    verification_labels=alert_context.verification_labels,
                    maintenance_capability=(
                        str(event.payload["maintenance_capability"])
                        if event.payload.get("maintenance_capability")
                        else None
                    ),
                    maintenance_parameters=(
                        {str(k): str(v) for k, v in parameters.items()}
                        if isinstance(
                            parameters := event.payload.get("maintenance_parameters"), dict
                        )
                        else {}
                    ),
                    alert_context=alert_context,
                )
        active: dict[str, str] = {}
        pending: list[AlertRepair] = []
        for fingerprint, event in latest.items():
            if event.type == ALERT_RESOLVED_EVENT:
                continue
            controller_id = str(event.payload.get("controller_id", ""))
            if controller_id:
                active[fingerprint] = controller_id
            if event.type == ALERT_QUEUED_EVENT:
                spec = queued.get(fingerprint)
                if spec is not None and controller_id and controller.get(controller_id) is not None:
                    pending.append(spec)
            elif event.type == ALERT_FINISHED_EVENT and event.payload.get("status") in {
                "failed",
                "timeout",
            }:
                spec = queued.get(fingerprint)
                if spec is not None and controller.get(spec.controller_id) is not None:
                    pending.append(spec)
        return active, pending

    async def schedule_alert(spec: AlertRepair) -> None:
        task = asyncio.create_task(process_alert_repair(spec))
        alert_tasks[spec.fingerprint] = task

        def consume_task_result(done: asyncio.Task[None]) -> None:
            if alert_tasks.get(spec.fingerprint) is done:
                alert_tasks.pop(spec.fingerprint, None)
            with suppress(asyncio.CancelledError):
                done.exception()

        task.add_done_callback(consume_task_result)

    async def current_alertmanager_fingerprints() -> set[str] | None:
        if not cfg.alertmanager_url:
            return None
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                response = await client.get(
                    f"{cfg.alertmanager_url.rstrip('/')}/api/v2/alerts"
                )
                response.raise_for_status()
                payload = response.json()
        except Exception:
            return None
        if not isinstance(payload, list):
            return None
        return {
            _alert_fingerprint(alert)
            for alert in payload
            if isinstance(alert, dict)
            and isinstance(alert.get("status"), dict)
            and alert["status"].get("state") == "active"
        }

    async def recover_alert_dispatcher_background() -> None:
        try:
            recovered, pending = await asyncio.to_thread(recoverable_alert_specs)
            current_fingerprints = await current_alertmanager_fingerprints()
            async with alert_lock:
                if current_fingerprints is None:
                    active_alerts.update(recovered)
                else:
                    for fingerprint, controller_id in recovered.items():
                        if fingerprint in current_fingerprints:
                            active_alerts[fingerprint] = controller_id
                            continue
                        await asyncio.to_thread(
                            runtime.store.append_event,
                            Event(
                                id=new_id("event"),
                                type=ALERT_RESOLVED_EVENT,
                                subject_ref=fingerprint,
                                payload={
                                    "fingerprint": fingerprint,
                                    "resolved_at": time.time(),
                                    "reconciled_from_alertmanager": True,
                                },
                            ),
                        )
                for spec in pending:
                    if (
                        current_fingerprints is not None
                        and spec.fingerprint in current_fingerprints
                        and spec.fingerprint not in alert_tasks
                    ):
                        await schedule_alert(spec)
        finally:
            recovery_complete.set()

    @app.on_event("startup")
    async def start_alert_dispatcher_recovery() -> None:
        nonlocal recovery_task
        recovery_complete.clear()
        recovery_task = asyncio.create_task(recover_alert_dispatcher_background())

    @app.on_event("shutdown")
    async def stop_alert_dispatcher() -> None:
        if metrics_refresh_task is not None:
            metrics_refresh_task.cancel()
            with suppress(asyncio.CancelledError):
                await metrics_refresh_task
        if environment_refresh_task is not None:
            environment_refresh_task.cancel()
            with suppress(asyncio.CancelledError):
                await environment_refresh_task
        if recovery_task is not None:
            recovery_task.cancel()
            with suppress(asyncio.CancelledError):
                await recovery_task
        tasks = list(alert_tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        alert_tasks.clear()

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
        game = (
            read_game_mode_state(
                cfg.game_mode_state_path,
                active_max_age_seconds=cfg.game_mode_active_max_age_seconds,
                restore_grace_seconds=cfg.game_mode_restore_grace_seconds,
            )
            if cfg.game_mode_enabled
            else None
        )
        game_mode_active = game is not None and game.suppress_alerts
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
                if (
                    game_mode_active
                    and name in {"prometheus", "alertmanager"}
                    and not checks[name]["ok"]
                ):
                    checks[name]["expected_down"] = True
        kernel = await kernel_health()
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
        expected_monitoring_down = game_mode_active and any(
            value.get("expected_down") is True for value in checks.values()
        )
        expected_unavailable = (
            expected_monitoring_down
            and set(unavailable_providers) <= {"personal-monitoring"}
        )
        checks_ok = all(
            value["ok"] or value.get("expected_down") is True
            for value in checks.values()
        )
        ready_ok = checks_ok and (
            not unavailable_providers or expected_unavailable
        )
        profile_metrics.record_readiness(ready_ok, kernel)
        body: dict[str, Any] = {
            "status": (
                "ok"
                if ready_ok
                else "degraded"
            ),
            "checks": checks,
            "kernel": kernel,
        }
        if expected_monitoring_down:
            body["expected_degraded"] = {
                "reason": "game_mode_active",
                "detail": (
                    "Prometheus/Alertmanager and the personal-monitoring provider "
                    "are expected to be unavailable while game mode stops containers."
                ),
            }
        if unavailable_providers:
            body["unavailable_providers"] = unavailable_providers
        if body["status"] == "ok":
            return body
        return JSONResponse(status_code=503, content=body)

    @app.get("/metrics", include_in_schema=False)
    async def metrics() -> Response:
        from agent_kernel.core import metrics as runtime_metrics

        if (
            metrics_content_cache is None
            or time.monotonic() - metrics_content_cache[0] >= 5
        ):
            schedule_metrics_refresh()
        if metrics_content_cache is None:
            content = b"".join(
                (generate_latest(), runtime_metrics.generate_metrics_content()[0])
            )
        else:
            content = metrics_content_cache[1]
        return Response(content=content, media_type=CONTENT_TYPE_LATEST)

    @app.get("/status")
    async def status_view(x_control_plane_key: str = Header(default="")) -> dict[str, Any]:
        require_key(x_control_plane_key)
        return {
            "status": "ok",
            "runtime": await kernel_health(),
            "diagnosis_model": cfg.diagnosis_model,
            "execution_model": cfg.execution_model,
        }

    @app.get("/v1/runtime")
    async def runtime_view(x_control_plane_key: str = Header(default="")) -> dict[str, Any]:
        require_key(x_control_plane_key)
        return {
            "runtime": await kernel_health(),
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
        with suppress(TimeoutError):
            await asyncio.wait_for(recovery_complete.wait(), timeout=1)
        accepted = suppressed = repaired = waiting = queued_count = deduplicated = 0
        controllers: list[str] = []
        for raw in alerts:
            if not isinstance(raw, dict):
                continue
            accepted += 1
            canonical_context = sanitize_alert_context(raw)
            labels = canonical_context.labels
            alert_status = canonical_context.status
            alertname = str(labels.get("alertname", "unknown"))
            job = str(labels.get("job", ""))
            fingerprint = _alert_fingerprint(raw)
            if alert_status == "resolved":
                async with alert_lock:
                    if fingerprint in active_alerts:
                        active_alerts.pop(fingerprint, None)
                        runtime.store.append_event(
                            Event(
                                id=new_id("event"),
                                type=ALERT_RESOLVED_EVENT,
                                subject_ref=fingerprint,
                                payload={
                                    "fingerprint": fingerprint,
                                    "alertname": alertname,
                                    "alert_context": canonical_context.to_payload(),
                                    "resolved_at": time.time(),
                                },
                            )
                        )
                continue
            if alert_status != "firing":
                continue
            game_mode_suppressible = alertname in cfg.game_mode_alertnames or (
                alertname == "PrometheusScrapeFailed"
                and job in cfg.game_mode_scrape_jobs
            )
            if game is not None and game.suppress_alerts and game_mode_suppressible:
                suppressed += 1
                continue
            project_label = str(labels.get("project", "")) or None
            verification_labels = canonical_context.verification_labels
            description = render_alert_context(canonical_context)
            async with alert_lock:
                existing_controller = active_alerts.get(fingerprint)
                if existing_controller:
                    deduplicated += 1
                    waiting += 1
                    controllers.append(existing_controller)
                    continue
            automatic_maintenance = (
                cfg.automatic_handling_enabled
                and alertname in cfg.auto_maintenance_alertnames
            )
            project = project_label if project_label in cfg.allowed_auto_projects else None
            line_ending_target: tuple[str, str] | None = None
            if (
                cfg.automatic_handling_enabled
                and alertname == _SYNC_ALERT_NAME
                and project_label in (None, "ratio")
            ):
                candidates = [
                    (str(raw_repo), candidate_project)
                    for raw_repo in cfg.line_ending_auto_discard_repos
                    if (candidate_project := cfg.auto_project_for_repo(raw_repo))
                    and candidate_project in cfg.allowed_auto_projects
                    and (project_label is None or candidate_project == project_label)
                ]
                if len(candidates) == 1:
                    line_ending_target = candidates[0]
                    project = line_ending_target[1]
            automatic_repair = cfg.automatic_handling_enabled and project is not None
            target_repo = (
                line_ending_target[0]
                if line_ending_target is not None
                else (cfg.project_dirs.get(project) if automatic_repair and project else None)
            )
            safety_hint = (
                "Historical safety hint only: this alert class normally requires extra safety "
                "review. Do not use the hint as the current safety decision; Codex must judge "
                "the current evidence.\n"
                if alertname in HISTORICAL_SAFETY_HINT_ALERT_NAMES
                else ""
            )
            spec = begin_alert_repair(
                fingerprint=fingerprint,
                title=f"Alert: {alertname}",
                description=(
                    safety_hint
                    + "Alertmanager canonical context (sanitized):\n"
                    + description
                ),
                repo=target_repo if automatic_repair else None,
                project=project if automatic_repair else None,
                verification_labels=verification_labels,
                alert_context=canonical_context,
                maintenance_capability=(
                    _LINE_ENDING_DISCARD_CAPABILITY
                    if line_ending_target is not None
                    else (
                        "maintenance.cleanup_known_garbage"
                        if automatic_maintenance
                        else None
                    )
                ),
                maintenance_parameters=(
                    {"repo": line_ending_target[0], "project": line_ending_target[1]}
                    if line_ending_target is not None
                    else {}
                ),
            )

            async with alert_lock:
                existing_controller = active_alerts.get(fingerprint)
                if existing_controller:
                    deduplicated += 1
                    waiting += 1
                    controllers.append(existing_controller)
                    continue
                append_alert_event(ALERT_QUEUED_EVENT, spec, queued_at=time.time())
                active_alerts[fingerprint] = spec.controller_id
                await schedule_alert(spec)
                queued_count += 1
                waiting += 1
                controllers.append(spec.controller_id)
        return AlertIngestResponse(
            accepted=accepted,
            suppressed=suppressed,
            repaired=repaired,
            waiting=waiting,
            controllers=controllers,
            queued=queued_count,
            deduplicated=deduplicated,
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
