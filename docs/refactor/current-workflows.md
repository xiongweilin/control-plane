# Current workflows

1. Alertmanager payload enters `RepairService.ingest`.
2. Alert policy/cooldown/budget/lease checks create a queued legacy repair.
3. Codex is invoked through `CodexRunner` and may execute registered tools.
4. `Verifier` produces a verification report.
5. Approval/rollback and notifications update repair state.
6. Candidate experience is stored in SQLite and can be promoted to a playbook.
7. Startup reconciles interrupted repairs and resumes approval/recovery work.

The first portable phase does not delete this path. It adds a generic Work/Run
path that can be exercised without Alertmanager, Codex, Feishu, Docker or
Prometheus.
