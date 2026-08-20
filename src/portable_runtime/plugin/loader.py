from __future__ import annotations

import json
from pathlib import Path

from portable_runtime.protocol.manifest import ProviderManifest


def load_manifest(path: Path) -> ProviderManifest:
    manifest_path = path / "manifest.json" if path.is_dir() else path
    with manifest_path.open(encoding="utf-8") as handle:
        return ProviderManifest.model_validate(json.load(handle))


def validate_manifest(path: Path) -> list[str]:
    try:
        manifest = load_manifest(path)
    except (OSError, ValueError) as exc:
        return [str(exc)]
    errors: list[str] = []
    if manifest.protocol_version != "1":
        errors.append("unsupported protocol_version")
    if not manifest.capabilities:
        errors.append("at least one capability is required")
    return errors
