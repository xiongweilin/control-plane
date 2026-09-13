from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any

from .alert_context import AlertContext, render_alert_context, sanitize_alert_context
from .audit import redact_value


@dataclass(frozen=True, slots=True)
class AlertRepair:
    fingerprint: str
    controller_id: str
    title: str
    description: str
    repo: str | None
    project: str | None
    verification_labels: dict[str, str]
    maintenance_capability: str | None
    maintenance_parameters: dict[str, str]
    alert_context: AlertContext = field(default_factory=AlertContext)


_DIAGNOSIS_SAFETY_RE = re.compile(
    r"(?im)^\s*SAFETY_CLASS\s*[=:]\s*(REVERSIBLE|IRREVERSIBLE|UNKNOWN)\s*$"
)


def _latest_alert_diagnosis_result(policy: Any, state: Any) -> dict[str, Any] | None:
    getter = getattr(policy, "_latest_diagnosis_result", None)
    if callable(getter) and state is not None:
        try:
            result = getter(state)
        except Exception:
            result = None
        if isinstance(result, dict):
            return dict(result)

    controller = getattr(policy, "controller", None)
    store = getattr(controller, "store", None)
    list_events = getattr(store, "list_events", None)
    if not callable(list_events) or state is None:
        return None
    try:
        events = list(list_events(state.id))
    except Exception:
        return None
    decisions: list[dict[str, Any]] = []
    for event in events:
        if getattr(event, "type", "") != "ControllerDecisionSelected":
            continue
        raw = getattr(event, "payload", {}).get("decision")
        if not isinstance(raw, dict):
            continue
        parameters = raw.get("parameters")
        if (
            raw.get("capability") == "reason.generate"
            and isinstance(parameters, dict)
            and parameters.get("phase") == "diagnosis"
        ):
            decisions.append(raw)
    if not decisions:
        return None
    decision_id = decisions[-1].get("id")
    for event in reversed(events):
        if getattr(event, "type", "") != "ControllerCapabilityResultObserved":
            continue
        payload = getattr(event, "payload", {})
        if payload.get("decision_ref") != decision_id:
            continue
        result = payload.get("result")
        return dict(result) if isinstance(result, dict) else None
    return None


def _fallback_policy_count(policy: Any, state: Any, phase: str) -> int:
    if state is None:
        return 0
    controller = getattr(policy, "controller", None)
    store = getattr(controller, "store", None)
    list_events = getattr(store, "list_events", None)
    if not callable(list_events):
        return 0
    try:
        events = list(list_events(state.id))
    except Exception:
        return 0
    if phase == "diagnosis":
        count = 0
        for event in events:
            if getattr(event, "type", "") != "ControllerDecisionSelected":
                continue
            raw = getattr(event, "payload", {}).get("decision")
            parameters = raw.get("parameters") if isinstance(raw, dict) else None
            if (
                isinstance(raw, dict)
                and raw.get("capability") == "reason.generate"
                and isinstance(parameters, dict)
                and parameters.get("phase") == "diagnosis"
            ):
                count += 1
        return count

    bridge = getattr(policy, "bridge", None)
    result_events = getattr(bridge, "result_events", None)
    if callable(result_events):
        try:
            return sum(
                1
                for event in result_events(state.id)
                if getattr(event, "payload", {}).get("stage") == phase
            )
        except Exception:
            return 0
    return 0


def _policy_count(policy: Any, state: Any, method_name: str, phase: str) -> int:
    method = getattr(policy, method_name, None)
    if callable(method) and state is not None:
        try:
            return max(0, int(method(state)))
        except Exception:
            pass
    return max(0, _fallback_policy_count(policy, state, phase))


def _diagnosis_status(
    result: dict[str, Any] | None, *, error: BaseException | None = None
) -> str:
    if result is None:
        error_text = f"{type(error).__name__} {error}".lower() if error else ""
        return "timeout" if "timeout" in error_text else "no_valid_diagnosis"

    status = str(result.get("status", "")).lower()
    message = str(result.get("message", ""))
    raw_error = result.get("error")
    error_text = (
        json.dumps(raw_error, ensure_ascii=False, default=str)
        if isinstance(raw_error, (dict, list))
        else str(raw_error or "")
    )
    combined = f"{message} {error_text}".lower()
    if status != "succeeded":
        if status in {"timeout", "timed_out"} or "timeout" in combined:
            return "timeout"
        if status == "malformed" or "malformed" in combined:
            return "malformed"
        return "provider_failed"
    return "valid" if len(_DIAGNOSIS_SAFETY_RE.findall(message)) == 1 else "malformed"


def _structured_text(value: Any, *, limit: int = 2000) -> str | None:
    if isinstance(value, str):
        value = value.strip()
        return value[:limit] or None
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, (dict, list)):
        return json.dumps(
            redact_value(value), ensure_ascii=False, sort_keys=True, default=str
        )[:limit]
    return None


def _first_structured_value(source: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = _structured_text(source.get(key))
        if value:
            return value
    metadata = source.get("metadata")
    if isinstance(metadata, dict):
        for key in keys:
            value = _structured_text(metadata.get(key))
            if value:
                return value
    return None


def _diagnosis_follow_up(
    policy: Any,
    state: Any,
    result: dict[str, Any] | None,
    spec: AlertRepair,
) -> tuple[str | None, str | None]:
    proposed_action = (
        _first_structured_value(
            result or {},
            ("proposed_action", "proposedAction", "action", "action_plan", "next_action"),
        )
        if result is not None
        else None
    )
    rollback = (
        _first_structured_value(
            result or {},
            ("rollback", "rollback_plan", "rollback_info", "rollback_action"),
        )
        if result is not None
        else None
    )

    controller = getattr(policy, "controller", None)
    store = getattr(controller, "store", None)
    list_events = getattr(store, "list_events", None)
    if not proposed_action and callable(list_events) and state is not None:
        try:
            events = list(list_events(state.id))
        except Exception:
            events = []
        for event in reversed(events):
            if getattr(event, "type", "") != "ControllerDecisionSelected":
                continue
            raw = getattr(event, "payload", {}).get("decision")
            closure = raw.get("closure") if isinstance(raw, dict) else None
            selected = closure.get("selected_direction") if isinstance(closure, dict) else None
            proposed_action = _structured_text(selected)
            if proposed_action:
                break

    if not proposed_action and spec.maintenance_capability:
        proposed_action = spec.maintenance_capability
    return proposed_action, rollback


def _alert_diagnosis_snapshot(
    policy: Any,
    state: Any,
    spec: AlertRepair,
    *,
    error: BaseException | None = None,
) -> dict[str, Any]:
    diagnosis_result = _latest_alert_diagnosis_result(policy, state)
    diagnosis_status = _diagnosis_status(diagnosis_result, error=error)
    try:
        safety_class = str(policy.safety_class(state)) if state is not None else "unknown"
    except Exception:
        safety_class = "unknown"
    diagnosis_attempts = _policy_count(policy, state, "_diagnosis_count", "diagnosis")
    diagnosis_completed_count = 0
    diagnosis_valid_count = 0
    controller = getattr(policy, "controller", None)
    store = getattr(controller, "store", None)
    list_events = getattr(store, "list_events", None)
    if callable(list_events) and state is not None:
        try:
            events = list(list_events(state.id))
        except Exception:
            events = []
        diagnosis_ids: set[str] = set()
        for event in events:
            if getattr(event, "type", "") != "ControllerDecisionSelected":
                continue
            raw = getattr(event, "payload", {}).get("decision")
            parameters = raw.get("parameters") if isinstance(raw, dict) else None
            if (
                isinstance(raw, dict)
                and raw.get("capability") == "reason.generate"
                and isinstance(parameters, dict)
                and parameters.get("phase") == "diagnosis"
                and isinstance(raw.get("id"), str)
            ):
                diagnosis_ids.add(raw["id"])
        for event in events:
            if getattr(event, "type", "") != "ControllerCapabilityResultObserved":
                continue
            payload = getattr(event, "payload", {})
            decision_ref = payload.get("decision_ref")
            result_payload = payload.get("result")
            if decision_ref not in diagnosis_ids or not isinstance(result_payload, dict):
                continue
            diagnosis_completed_count += 1
            if _diagnosis_status(result_payload) == "valid":
                diagnosis_valid_count += 1
    observed_execution_attempts = _policy_count(policy, state, "_execution_count", "execution")
    execution_attempts = observed_execution_attempts if diagnosis_status == "valid" else 0
    try:
        blocker = policy.diagnosis_blocker(state) if state is not None else None
    except Exception:
        blocker = None
    proposed_action, rollback = _diagnosis_follow_up(policy, state, diagnosis_result, spec)
    return {
        "diagnosis_status": diagnosis_status,
        "safety_class": safety_class,
        "diagnosis_attempts": diagnosis_attempts,
        "diagnosis_requested_count": diagnosis_attempts,
        "diagnosis_completed_count": diagnosis_completed_count,
        "diagnosis_valid_count": diagnosis_valid_count,
        "execution_attempts": execution_attempts,
        "blocker": blocker,
        "proposed_action": proposed_action,
        "rollback": rollback,
    }


def _alert_escalation_reason(snapshot: dict[str, Any], fallback: str) -> str:
    diagnosis_status = str(snapshot.get("diagnosis_status", "no_valid_diagnosis"))
    if diagnosis_status != "valid":
        return diagnosis_status
    blocker = snapshot.get("blocker")
    return str(blocker) if blocker else fallback


def _format_alert_escalation(
    spec: AlertRepair,
    *,
    work_id: str,
    controller_id: str,
    controller_status: str,
    snapshot: dict[str, Any],
) -> str:
    context = spec.alert_context
    labels = context.labels
    annotations = context.annotations
    diagnosis_status = str(snapshot.get("diagnosis_status", "no_valid_diagnosis"))
    if diagnosis_status != "valid":
        headline = (
            f"告警未获得有效 diagnosis ({diagnosis_status}), 已停止自动重试;"
            "这不表示 Codex 已作出判断。\n"
        )
    elif snapshot.get("blocker") == "irreversible":
        headline = "首轮有效 diagnosis 判定该告警对应操作不可逆, 已停止自动 effect。\n"
    elif snapshot.get("blocker") == "dirty-repository":
        headline = "首轮有效 diagnosis/现场核验发现目标仓库不干净, 已停止自动 effect。\n"
    elif (
        int(snapshot.get("diagnosis_attempts", 0)) >= 2
        and int(snapshot.get("execution_attempts", 0)) >= 2
    ):
        headline = "告警经过两轮有效 diagnosis 与执行后仍未解除, 已停止自动重试。\n"
    else:
        headline = "告警已有有效 diagnosis 但仍未解除, 已停止自动重试。\n"

    lines = [
        headline,
        "通知送达状态由 provider 结果单独记录; control-plane 不证明最终送达。",
        f"canonical_alert_context={render_alert_context(context)}",
        f"alert={spec.title}",
        f"status={context.status}",
        f"alertname={labels.get('alertname', 'unknown')}",
        f"summary={annotations.get('summary', '<not provided>')}",
        f"description={annotations.get('description', '<not provided>')}",
        f"detail={annotations.get('detail', '<not provided>')}",
        f"controller_status={controller_status}",
        f"work={work_id}",
        f"controller={controller_id}",
        f"diagnosis_status={diagnosis_status}",
        f"safety_class={snapshot.get('safety_class', 'unknown')}",
        f"diagnosis_attempts={snapshot.get('diagnosis_attempts', 0)}",
        f"diagnosis_requested_count={snapshot.get('diagnosis_requested_count', 0)}",
        f"diagnosis_completed_count={snapshot.get('diagnosis_completed_count', 0)}",
        f"diagnosis_valid_count={snapshot.get('diagnosis_valid_count', 0)}",
        f"execution_attempts={snapshot.get('execution_attempts', 0)}",
        f"blocker={snapshot.get('blocker') or 'none'}",
        f"proposed_action={snapshot.get('proposed_action') or 'unavailable'}",
        f"rollback={snapshot.get('rollback') or 'unavailable'}",
    ]
    for key in ("instance", "path", "project"):
        value = labels.get(key)
        if value:
            lines.append(f"{key}={value}")
    if context.observed_at:
        lines.append(f"observed_at={context.observed_at}")
    lines.append(f"继续命令: /task {controller_id} <明确命令>")
    return "\n".join(lines)


def _alert_fingerprint(raw: dict[str, Any]) -> str:
    supplied = raw.get("fingerprint")
    if isinstance(supplied, str) and supplied.strip():
        return f"alertmanager:{supplied.strip()[:200]}"
    labels = sanitize_alert_context(raw).labels
    stable_labels = {
        key: str(labels[key])
        for key in ("alertname", "job", "project", "instance", "severity")
        if key in labels
    }
    canonical = json.dumps(stable_labels, ensure_ascii=False, sort_keys=True)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"labels:{digest}"
