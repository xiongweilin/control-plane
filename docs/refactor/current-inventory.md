# Current inventory (2026-08-20)

This snapshot is based on the repository contents, not the README. The legacy
package is `src/control_plane` and exposes a FastAPI app from `control_plane.app:create_app`.

## Entrypoints and loops

- `control_plane.__main__`: uvicorn serving, candidate cleanup, session inspection.
- `control_plane.app`: liveness/readiness/metrics, Alertmanager webhook, approvals,
  task dispatch, digest/scan/model-recovery background loops.
- `control_plane.service.RepairService`: alert ingestion, repair lifecycle,
  verification, candidate experience and reconciliation.
- `control_plane.codex_runner`: subprocess Codex execution and session artifacts.

## Persistence

`control_plane.storage.Store` owns a SQLite database with tables for alerts,
repairs, actions, approvals, candidates, playbooks, budget usage, run records,
command audit, leases and settings. Evidence/session files live under the
configured data directory.

## External boundaries

- Codex CLI and local model gateway.
- Prometheus and Alertmanager HTTP endpoints.
- Docker/compose subprocess tools.
- Git subprocesses and optional SSH fallback.
- Feishu notification script.
- Windows PowerShell/VBS scheduled-task wrappers under `scripts/`.

## Lifecycle and contracts

Repair lifecycle is defined by `control_plane.state_machine.RepairState` and
legacy repair IDs. Alertmanager payloads are parsed by `control_plane.models`.
HTTP routes are concentrated in `control_plane.app` and currently use the
legacy `/healthz`, `/live`, `/ready`, `/status`, `/v1/...` surface.

The new `portable_runtime` package is introduced as an additive seam. Legacy
code remains the reference profile until parity tests exist.
