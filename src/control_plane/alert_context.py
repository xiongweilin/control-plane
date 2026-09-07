"""Canonical, bounded Alertmanager context for the control-plane profile.

Alert ingress is the only place where an untrusted Alertmanager payload is
converted into the structured context used by the journal, Codex, verification
and escalation surfaces.  Historical journal records are adapted through the
same type so old events do not create a second alert representation.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from .audit import redact_value

_LABEL_FIELDS = ("alertname", "job", "project", "path", "instance", "severity")
_ANNOTATION_FIELDS = ("summary", "description", "detail")
_OBSERVED_AT_FIELDS = (
    "observed_at",
    "observedAt",
    "last_observed_at",
    "lastObservedAt",
    "last_seen",
    "lastSeen",
)


def _bounded_mapping(
    value: Any,
    allowed: tuple[str, ...] | None = None,
    *,
    value_limit: int,
) -> dict[str, str]:
    if not isinstance(value, Mapping):
        return {}
    keys = allowed if allowed is not None else tuple(str(key) for key in value)
    return {
        key: str(value[key])[:value_limit]
        for key in keys
        if key in value
    }


@dataclass(frozen=True, slots=True)
class AlertContext:
    """The one sanitized alert representation shared by all control-plane stages."""

    status: str = "firing"
    labels: dict[str, str] = field(default_factory=dict)
    annotations: dict[str, str] = field(default_factory=dict)
    observed_at: str | None = None

    @classmethod
    def from_raw(cls, raw: Mapping[str, Any] | None) -> AlertContext:
        raw = raw if isinstance(raw, Mapping) else {}
        raw_annotations = raw.get("annotations")
        labels = _bounded_mapping(raw.get("labels"), _LABEL_FIELDS, value_limit=500)
        annotations = _bounded_mapping(
            raw_annotations,
            _ANNOTATION_FIELDS,
            value_limit=2000,
        )
        observed_at: str | None = None
        for key in _OBSERVED_AT_FIELDS:
            value = raw.get(key)
            if value is None and isinstance(raw_annotations, Mapping):
                value = raw_annotations.get(key)
            if isinstance(value, (str, int, float)) and str(value).strip():
                observed_at = str(value)[:200]
                break
        sanitized = redact_value(
            {
                "status": str(raw.get("status", "firing"))[:50],
                "labels": labels,
                "annotations": annotations,
                **({"observed_at": observed_at} if observed_at else {}),
            }
        )
        return cls(
            status=str(sanitized.get("status", "firing")),
            labels=dict(sanitized.get("labels", {})),
            annotations=dict(sanitized.get("annotations", {})),
            observed_at=(
                str(sanitized["observed_at"])
                if sanitized.get("observed_at") is not None
                else None
            ),
        )

    @classmethod
    def from_event_payload(cls, payload: Mapping[str, Any]) -> AlertContext:
        """Load current or historical event data without inventing new fields."""

        raw_context = payload.get("alert_context")
        if isinstance(raw_context, Mapping):
            return cls.from_raw(raw_context)

        description = str(payload.get("description", ""))
        candidate = description[description.find("{") :] if "{" in description else ""
        if candidate:
            try:
                parsed = json.loads(candidate)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, Mapping):
                return cls.from_raw(parsed)

        # Legacy queued events stored verification labels separately.  Keep
        # those labels bounded while making the result the same canonical type.
        return cls.from_raw(
            {
                "status": "firing",
                "labels": payload.get("verification_labels"),
                "annotations": {"description": description} if description else {},
            }
        )

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "status": self.status,
            "labels": dict(self.labels),
            "annotations": dict(self.annotations),
        }
        if self.observed_at:
            payload["observed_at"] = self.observed_at
        return payload

    def render(self, *, limit: int = 12_000) -> str:
        return json.dumps(
            self.to_payload(),
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )[:limit]

    @property
    def alertname(self) -> str:
        return self.labels.get("alertname", "unknown")

    @property
    def severity(self) -> str:
        return self.labels.get("severity", "")

    @property
    def summary(self) -> str:
        return self.annotations.get("summary", "")

    @property
    def description(self) -> str:
        return self.annotations.get("description", "")

    @property
    def detail(self) -> str:
        return self.annotations.get("detail", "")

    @property
    def verification_labels(self) -> dict[str, str]:
        """Return the bounded label projection used by policy/verification."""

        return {
            key: self.labels[key]
            for key in ("alertname", "job", "project", "path", "instance")
            if self.labels.get(key)
        }


def sanitize_alert_context(raw: Mapping[str, Any] | None) -> AlertContext:
    return AlertContext.from_raw(raw)


def render_alert_context(context: AlertContext) -> str:
    return context.render()


def alert_context_from_event_payload(payload: Mapping[str, Any]) -> AlertContext:
    return AlertContext.from_event_payload(payload)
