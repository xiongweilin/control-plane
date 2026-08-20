from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping

from .models import Alert

KEY_LABELS = ("alertname", "instance", "job", "project", "container", "name")


def fingerprint_from_labels(labels: Mapping[str, str]) -> str:
    """Stable fingerprint over the key labels, independent of the Alert shape."""
    material = {key: labels.get(key, "") for key in KEY_LABELS}
    payload = json.dumps(material, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def alert_fingerprint(alert: Alert) -> str:
    """Stable fingerprint for deduplication and cooldown decisions."""
    return fingerprint_from_labels(alert.labels)


def alert_key_labels(alert: Alert) -> dict[str, str]:
    return {key: alert.labels.get(key, "") for key in KEY_LABELS if alert.labels.get(key)}


def fingerprint_pattern(alert: Alert) -> str:
    """Pattern used for candidate matching; instance is normalized to a wildcard."""
    labels = alert_key_labels(alert)
    name = labels.get("alertname", "unknown")
    project = labels.get("project", "*")
    container = labels.get("container", "*")
    return f"{name}|{project}|{container}"
