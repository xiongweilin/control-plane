"""Portable local deployment factory and Windows profile adapter (B4)."""

from __future__ import annotations

from pathlib import Path

from portable_runtime.core.runtime import Runtime
from portable_runtime.stores.filesystem import FilesystemArtifactStore
from portable_runtime.stores.sqlite import SQLiteStateStore


def create_local_runtime(state_path: Path, artifact_root: Path | None = None) -> Runtime:
    """Create the minimal portable-local runtime (no Codex/Feishu/Docker required)."""
    return Runtime(
        store=SQLiteStateStore(state_path),
        artifact_store=FilesystemArtifactStore(artifact_root) if artifact_root else None,
        runtime_id="portable-local",
    )


def create_personal_platform_runtime(state_path: Path, artifact_root: Path | None = None) -> Runtime:
    """Create the legacy personal-platform profile runtime (with Codex/Feishu capabilities available)."""
    # In this phase the profile reuses the same SQLite store but documents the
    # intended provider set. Actual Codex/Feishu provider registration is done
    # by the deployment entrypoint that loads `control_plane.toml` or `portable_runtime.toml`.
    runtime = create_local_runtime(state_path, artifact_root)
    runtime.runtime_id = "personal-platform"
    return runtime
