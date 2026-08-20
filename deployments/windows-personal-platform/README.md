# Windows Personal Platform profile

This directory preserves the legacy Windows deployment: Task Scheduler, PowerShell supervisor, VBS wrapper, watchdog, Prometheus/Alertmanager, Docker, Feishu.

In the portable refactoring, this profile becomes `profiles/personal-platform` equivalent:

- AlertmanagerTrigger
- CodexProvider (codex exec)
- PrometheusProvider / DockerProvider (verifiers)
- Feishu providers
- SQLite stores
- IncidentRepairWorkflow + DailyScanWorkflow + KnowledgeConsolidationWorkflow
- legacy policies

No code change in this phase; the directory documents the migration target per §56-§57.

Legacy scripts remain under `scripts/` until the profile is fully cut over.
