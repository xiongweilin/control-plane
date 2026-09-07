from __future__ import annotations

from control_plane.alert_context import (
    AlertContext,
    alert_context_from_event_payload,
    sanitize_alert_context,
)


def test_alert_context_is_bounded_redacted_and_reused_as_projection() -> None:
    context = sanitize_alert_context(
        {
            "status": "firing",
            "labels": {
                "alertname": "ControlPlaneSynchronizationDegraded",
                "instance": "node-01",
                "ignored": "must not escape",
            },
            "annotations": {
                "summary": "summary",
                "description": "description",
                "detail": "detail",
                "api_key": "secret-value",
            },
            "observed_at": "2026-09-07T08:00:00+08:00",
        }
    )

    assert context == AlertContext(
        status="firing",
        labels={
            "alertname": "ControlPlaneSynchronizationDegraded",
            "instance": "node-01",
        },
        annotations={
            "summary": "summary",
            "description": "description",
            "detail": "detail",
        },
        observed_at="2026-09-07T08:00:00+08:00",
    )
    assert context.verification_labels == {
        "alertname": "ControlPlaneSynchronizationDegraded",
        "instance": "node-01",
    }
    assert "api_key" not in context.render()
    assert context.render() == AlertContext.from_raw(context.to_payload()).render()


def test_historical_event_context_uses_same_canonical_type() -> None:
    context = alert_context_from_event_payload(
        {
            "description": '{"status":"firing","labels":{"alertname":"Legacy"}}',
            "verification_labels": {"alertname": "WrongLegacyProjection"},
        }
    )

    assert isinstance(context, AlertContext)
    assert context.alertname == "Legacy"
    assert context.verification_labels == {"alertname": "Legacy"}
