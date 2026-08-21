# Current dependency map

## Core legacy package

`control_plane.service` still owns the personal integration adapters for
storage, Codex execution, verifier, tools, notifications and alert parsing.
Git push is implemented by the private typed personal-operations provider and
is reached through `PortableRuntimeAuthority`; the service does not perform a
raw merge/push or Docker lifecycle call in the production app path.

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

The compatibility-only `RepairService` constructor can be used without a
portable Runtime by legacy tests and callers. That path still uses the same
typed personal provider directly; `create_app` always supplies the private
Runtime and therefore enforces the full RealityBoundary, authorization and
procedure gates.
