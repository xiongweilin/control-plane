# Profile: personal-platform (Windows)

This profile is the Windows personal-platform deployment that preserves the
Codex / Feishu / Prometheus / Alertmanager / Docker integrations while using
Portable Runtime for canonical Work/Run orchestration.

It is the reference deployment that proves the portable Runtime can load the
same Work/Run/Artifact/Evidence/Knowledge with a different provider set.

Canonical location: `deployments/windows-personal-platform/` (physical files).
This `profiles/personal-platform/` directory is the portable alias per §57;
both refer to the same set:

- AlertmanagerTrigger -> Work(kind="incident") -> IncidentRepairWorkflow
- CodexProvider (codex exec)
- PrometheusProvider / DockerProvider (verifiers)
- FeishuTrigger + FeishuHumanProvider + FeishuNotificationProvider
- SQLiteStateStore + FilesystemArtifactStore
- DailyScanWorkflow + KnowledgeConsolidationWorkflow
- personal policy adapters and legacy compatibility APIs

Codex is capability-scoped: reasoning/read/diff capabilities use a
`read-only` sandbox, candidate edits/tests use `workspace-write`, and unknown
capabilities fail closed. Remote or deployment effects must use an independent
Provider governed by Portable Runtime's RealityBoundary.

For the cross-platform counterpart with no Windows/Docker dependency, see
`deployments/portable-local/` and `docs/deployment-local.md`.

Task Scheduler / PowerShell / VBS / watchdog scripts live in
`deployments/windows-personal-platform/` (copied from `scripts/` per §56).
This profile never leaks into `portable_runtime/core`.
