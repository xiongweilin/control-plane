from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class EvidenceRecord:
    record_type: str
    scope: str
    epistemic_status: str
    lifecycle_status: str
    created_by: str = "control-plane"
    system_boundary: str = "control-plane"
    source_refs: list[str] = field(default_factory=list)
    environment_versions: dict[str, str] = field(default_factory=dict)
    verification_refs: list[str] = field(default_factory=list)
    supersedes: str | None = None
    assumptions: list[str] = field(default_factory=list)
    known_limitations: list[str] = field(default_factory=list)
    unknown_scopes: list[str] = field(default_factory=list)
    possibility_space_ref: str | None = None
    invalidation_conditions: list[str] = field(default_factory=list)
    valid_from: str | None = None
    expires_at: str | None = None
    review_triggers: list[str] = field(default_factory=list)
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["id"] = f"ev-{uuid.uuid4().hex[:12]}"
        data["created_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        return data


def write_evidence(evidence_dir: Path, repair_id: str, record: EvidenceRecord) -> Path:
    repair_dir = evidence_dir / repair_id
    repair_dir.mkdir(parents=True, exist_ok=True)
    sequence = len(list(repair_dir.glob("*.json"))) + 1
    path = repair_dir / f"{sequence:03d}-{record.record_type}.json"
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(record.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(temporary, path)
    return path
