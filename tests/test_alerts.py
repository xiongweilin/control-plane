from __future__ import annotations

from control_plane.alerts import alert_fingerprint, fingerprint_pattern
from control_plane.models import Alert


def _alert(**labels: str) -> Alert:
    return Alert.model_validate(
        {
            "status": "firing",
            "labels": labels or {"alertname": "Test"},
            "annotations": {"summary": "s"},
            "startsAt": "2026-08-06T00:00:00Z",
            "endsAt": None,
            "fingerprint": "f1",
        }
    )


def test_fingerprint_is_stable_and_ignores_annotations() -> None:
    first = _alert(alertname="HighCPU", instance="node1", project="dify")
    second = _alert(alertname="HighCPU", instance="node1", project="dify")
    second.annotations["description"] = "changed"
    assert alert_fingerprint(first) == alert_fingerprint(second)
    assert alert_fingerprint(first) != alert_fingerprint(_alert(alertname="HighCPU", instance="node2", project="dify"))


def test_fingerprint_pattern_normalizes_instance() -> None:
    assert fingerprint_pattern(_alert(alertname="HighCPU", instance="a", project="dify")) == "HighCPU|dify|*"
