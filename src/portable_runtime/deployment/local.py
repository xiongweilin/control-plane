from __future__ import annotations

from pathlib import Path

from portable_runtime.core.runtime import Runtime
from portable_runtime.stores.filesystem import FilesystemArtifactStore
from portable_runtime.stores.sqlite import SQLiteStateStore


def create_local_runtime(state_path: Path, artifact_root: Path | None = None) -> Runtime:
    return Runtime(
        store=SQLiteStateStore(state_path),
        artifact_store=FilesystemArtifactStore(artifact_root) if artifact_root else None,
        runtime_id="portable-local",
    )

