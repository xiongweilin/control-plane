# Deployment: Windows personal platform

The original Windows deployment is preserved as `deployments/windows-personal-platform/` and `profiles/personal-platform` equivalent.

Components preserved:

```
AlertmanagerTrigger -> Work(kind="incident") -> IncidentRepairWorkflow
CodexProvider (codex exec --model from [[providers]] / [agent] model)
Prometheus / Docker verifiers (verify.promql / verify.container / verify.http)
FeishuTrigger + FeishuHumanProvider + FeishuNotificationProvider
SQLiteStateStore + FilesystemArtifactStore
Legacy policies: SensitivePathPolicy, ExternalSideEffectPolicy, CandidateMergePolicy
```

## Codex process boundary

The private launcher keeps the Codex session separate from host control-plane
credentials. With the default `[agent]` settings:

- `workspace-write` starts in a detached candidate worktree under
  `worktree_root`; the source checkout is not the child cwd and the candidate
  branch is retained after cleanup for approval;
- `disable_ssh_credentials = true` removes Git/SSH agent and token variables,
  disables credential helpers, and installs deny-only SSH/askpass commands;
- `disable_docker = true` points Docker at the nonexistent
  `control-plane-codex-disabled` named-pipe/context and an empty Docker config.

The Git merge/push and Docker restart/compose providers are separate
Runtime-authorized processes. Do not turn these switches off for production
repair sessions unless a deployment-specific restricted Windows account/ACL
has been verified first.

Scripts still under `scripts/`:

- `install-control-plane.ps1` – registers the Scheduled Task
- `Run-ControlPlane.ps1` – supervisor (single-instance guard, PID file, restart recovery)
- `check_coverage.py` / `check_portable_core_imports.py` – CI gates

Cut-over plan (§56-§57): move `Task Scheduler / PowerShell / VBS / watchdog` into this profile, keep `portable_runtime` import-free. The portable-local profile (`uv run python -m portable_runtime`) must never require Windows-only code.

See `docs/architecture.md` for the overall structure and `docs/state-migration.md` for export/import across Windows -> Linux.
