# Current dependency map

## Core legacy package

`control_plane.service` imports storage, Codex execution, verifier, tools,
notifications, git push and alert parsing directly. `control_plane.app` wires
all of those implementations and performs startup reconciliation.

## Runtime-specific assumptions

- Windows paths and PowerShell/VBS wrappers are present in configuration and
  deployment scripts.
- Prometheus/Alertmanager are readiness and trigger integrations.
- Codex CLI/model gateway are the current reasoning and coding entry points.
- SQLite and local filesystem are the only state/artifact backends.

## Portable seam introduced in this change

`portable_runtime.core` depends only on canonical Pydantic models, stable
interfaces, a registry and deterministic routing. Provider, trigger, storage,
HTTP and CLI implementations are outside core. The import boundary is checked
by `scripts/check_portable_core_imports.py`.
