# Windows Personal Platform profile

This directory preserves the legacy Windows deployment: Task Scheduler, PowerShell supervisor, VBS wrapper, watchdog, Prometheus/Alertmanager, Docker, Feishu.

In the portable refactoring, this profile becomes `profiles/personal-platform` equivalent (§56-§57):

- AlertmanagerTrigger -> Work(kind="incident") -> IncidentRepairWorkflow
- CodexProvider (codex exec --model from [[providers]] / [agent] model)
- Prometheus / Docker verifiers (verify.promql / verify.container / verify.http)
- FeishuTrigger + FeishuHumanProvider + FeishuNotificationProvider
- SQLiteStateStore + FilesystemArtifactStore
- Legacy policies: SensitivePathPolicy, ExternalSideEffectPolicy, CandidateMergePolicy

## Files in this profile

- `install-control-plane.ps1` — registers the Windows Scheduled Task (Task Scheduler) that launches the supervisor on logon/boot
- `Run-ControlPlane.ps1` — PowerShell supervisor: single-instance guard, PID file, restart recovery, model-gateway preflight, failure-driven recovery loop every `model_recovery_retry_seconds`
- `Run-ControlPlaneHidden.vbs` — VBS wrapper to run the supervisor hidden (no console window)
- `Watch-ControlPlane.ps1` / `Watch-ControlPlaneHidden.vbs` — watchdog that restarts the platform if the HTTP probe fails

These files were moved from `scripts/` into this profile per §56. The originals are retained in `scripts/` for backwards compatibility during the cut-over; the canonical location is this directory.

## Portable counterpart

For a cross-platform deployment that needs no Windows, Codex, Feishu, Docker or Prometheus, see `deployments/portable-local/`:

```powershell
uv sync
uv run python -m portable_runtime --state data/portable-runtime.db status
```

Or via Docker (Core does not depend on Docker, Dockerfile at repo root only wraps the same entrypoint):

```powershell
docker build -t portable-runtime:local .
docker run --rm portable-runtime:local python -m portable_runtime --state /data/portable-runtime.db status
```

## Trigger replacement (§14)

Windows Task Scheduler is replaced inside the Runtime by:

- `src/portable_runtime/triggers/schedule/trigger.py` — `ScheduleTrigger` (asyncio `asyncio.sleep` loop, `interval_seconds`, `start(emit)` / `stop()` / `emit_once()`)
- External cron can also trigger via `POST /v1/triggers/schedule/emit` or `POST /v1/triggers/webhook`

Core never imports Task Scheduler, PowerShell, VBS or Docker; those remain only in this deployment profile.

See `docs/deployment-windows-personal-platform.md`, `docs/architecture.md` and `docs/state-migration.md` for migration across Windows -> Linux.

