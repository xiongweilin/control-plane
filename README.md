# control-plane

Personal Platform Runtime Profile for `portable-runtime`. The repository
keeps the Windows/Feishu/Prometheus integration and the legacy HTTP/CLI
compatibility surface, while the portable Work/Run/Provider/Workflow runtime
is vendored from `ratiolin/portable-runtime`.

## Runtime identity

| Axis | Value |
| --- | --- |
| Framework semantics | `1.0.0` |
| Control Plane schema | `official-1.0.0` |
| Portable Runtime milestone | `R2.0` |
| Runtime protocol | `2.0` |
| Portable Runtime pin | `8d67339` |
| Personal profile | `P1.x` |

`src/portable_runtime` follows the public pin as its base. The provider-neutral
capability, procedure-profile, qualification, store, and Codex sandbox
semantics are synchronized to public pin `8d67339`; the remaining private
provider difference is the Windows execution boundary adapter.

[![CI](https://github.com/ratiolin/control-plane/actions/workflows/ci.yml/badge.svg)](https://github.com/ratiolin/control-plane/actions/workflows/ci.yml) [![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE) [![Python 3.12](https://img.shields.io/badge/Python-3.12-blue.svg)](pyproject.toml)


## Portable Runtime quick start (no Codex / Feishu / Docker / Prometheus required)

```powershell
uv sync
uv run runtime init
uv run runtime start
```

In another terminal:

```powershell
uv run runtime provider list
uv run runtime plugin install examples/echo-provider
uv run runtime work submit --kind generic-task --title "Echo test" --capability text.echo --description "hello"
uv run runtime work list
# also via python module:
# .venv\Scripts\python.exe -m portable_runtime --state data/portable-runtime.db status
# .venv\Scripts\python.exe -m portable_runtime plugin validate examples/echo-provider
# .venv\Scripts\python.exe -m portable_runtime work submit --title "Echo test" --description "hello" --kind generic-task --capability text.echo
```

The runtime stores Work/Run history and can export/import state without any model, harness, shell, browser, verifier, human channel or network connection:

```powershell
.venv\Scripts\python.exe -m portable_runtime --state data/portable-runtime.db state export runtime-state.json
.venv\Scripts\python.exe -m portable_runtime --state data/portable-runtime.db state import runtime-state.json
```

See [docs/architecture.md](docs/architecture.md),
[docs/provider-api.md](docs/provider-api.md),
[docs/provider-protocol.md](docs/provider-protocol.md),
[docs/plugin-authoring.md](docs/plugin-authoring.md),
[docs/workflow-authoring.md](docs/workflow-authoring.md),
[docs/store-api.md](docs/store-api.md),
[docs/state-migration.md](docs/state-migration.md) and
[docs/deployment-local.md](docs/deployment-local.md).

---

> [!WARNING]
> `portable_runtime` is the public, provider-neutral runtime base. The private
> `control_plane` project is the personal-platform superset: it adds the
> Windows/Feishu/Prometheus integrations and remains the production entrypoint.

## Personal Platform Profile (Windows/Feishu/Prometheus/Docker integration)

The `control_plane` package below is the private personal-platform profile
over the public portable runtime. It handles the personal integrations while
the portable runtime provides the canonical Work/Run and provider-neutral
capability foundation. Only this section requires Codex, Feishu, Prometheus,
Alertmanager, Docker or Windows Task Scheduler.

## Architecture

```text
Prometheus → Alertmanager
              └─ webhook → control-plane :18083 (Windows resident)
                              ├─ fingerprint dedup / cooldown / budget
                              ├─ codex agent session (capability-scoped sandbox)
                              ├─ independent verifier + auto rollback
                              ├─ SQLite + data/evidence/*.json
                              └─ Feishu notification / approval (feishu-dify-gateway extended commands)
```

## Quick start

```powershell
uv sync --extra dev
uv run ruff check .
uv run pytest
Copy-Item control_plane.toml.example control_plane.toml
[Environment]::SetEnvironmentVariable('CONTROL_PLANE_API_KEY', '<random key>', 'User')
uv run python -m control_plane
```

## Permission matrix

- Codex capability requests use a physical sandbox ceiling: `reason.generate`, `code.read` and `git.diff` run `read-only`; `code.edit`, `code.test` and `shell.exec` run `workspace-write`. Unknown capabilities fail closed to `read-only`; `danger-full-access` is not a personal-profile sandbox.
- A repair session requests `code.edit` explicitly and runs from a detached candidate Git worktree by default (`[agent] isolate_worktree = true`); the source checkout is never the Codex cwd. The candidate branch remains in the source repository for review after the ephemeral worktree is removed.
- Remote/deployment effects are not delegated to the Codex session. They require a separate Provider behind Portable Runtime's RealityBoundary and its authorization/reliability checks.
- Runtime operations are separate from Codex: `git.merge` / `git.push` and `docker.restart` / `docker.compose.up` are private typed providers behind Portable Runtime authorization, procedure and reliability gates. The Codex session may propose these actions but cannot execute them directly. Docker results keep desired-state evidence separate from event attribution: `docker.restart` remains non-terminal (`unknown`) when the command returned and the allowlisted project is healthy but no independent restart identity was observed; `docker.compose.up` is explicitly a desired-state operation.
- Candidate + approval: agent modifications to code/config must be committed to the `fix/control-plane-<id>` branch; the control plane reads the branch diff, refuses changes to verifiers/alert rules/permissions/the control plane itself, and after approval invokes the separately authorized Git provider. Candidate promotion additionally requires a closed source repair and records a typed portable `Decision` + `AuthorizationGrant` scoped to the candidate version/resource. If the agent times out but has left a commit, the repair enters `candidate/pending-review` (repair state `needs_approval`) and is not marked failed; after every agent run, `finally` restores the original Git branch.
- Denied by default: file writes (except candidate branches), dependency changes, database writes, cloud writes, credential access, data deletion, and modifying verifiers/alert rules/permissions.

## Agent trigger notes

The control plane starts the Codex CLI to run a full agent session. Executable resolution priority: explicit `[agent] codex_cli` config > `codex` on PATH (scoop shim) > bare `codex`. Before startup / each session it runs `codex --version` preflight: a missing CLI or failed probe is rejected with a clear error, and the last recorded version change is written to `codex:cli_version` and exposed through the `control_plane_codex_cli_info` metric.

```text
codex exec --model <model> --sandbox <read-only|workspace-write> --skip-git-repo-check --json <task prompt>
  # cwd = detached candidate worktree for workspace-write; transcript on stdout
```

The working directory uses native Windows paths (WSL was retired on 2026-08-07). The private app passes this boundary configuration into the actual vendored `CodexProvider` used by alert-driven repairs; the compatibility `CodexRunner` uses the same helper. Each child environment removes GitHub/SSH token variables, disables Git credential helpers and SSH-agent inheritance, and sets `GIT_SSH_COMMAND`/`GIT_ASKPASS` to non-interactive deny commands. Docker variables point to a deliberately nonexistent named pipe/context (`control-plane-codex-disabled`), so the Codex process does not inherit Docker Desktop's host control plane. These guards are independent of the prompt and the Codex sandbox; typed Git/Docker providers use their own authority-scoped process environment. The control plane injects hard constraints, verifies results independently, gates code-merge approval, and performs rollback. The Codex sandbox is a separate physical ceiling, not a substitute for Portable Runtime authorization or RealityBoundary governance.

## Feishu commands

```text
/cp status
/cp approve <repair_id> | reject <repair_id> | rollback <repair_id>
/cp policy <fingerprint> auto|manual|ignore
/cp run <fingerprint>
/cp ignore <fingerprint>
/cp evidence
/cp pause | resume
/cp promote <candidate_id>
/cp dismiss <candidate_id>
/task <description> dispatch a task to the Agent for execution
```

An ordinary Feishu message (not a command) is equivalent to `/task <description>` and dispatches directly to the control plane's Codex Agent; the Dify Chatflow has been removed. `/task <description>` dispatches the task to the control plane's Codex Agent (the model is fixed by `control_plane.toml [agent] model`), and the execution process is pushed too: task received → Agent started → outcome recorded → delivery/objective verification status.

### Task verification scope

`/task` keeps the user's natural-language request as the `Work` objective.
The default `generic-task` postcondition is delivery-scoped: it proves only
that a readable result artifact was produced and bound to the canonical
`Work`/`Run`. A provider `exit 0`, a transcript, or a run-associated artifact
does not prove that the objective in `Work.description` was achieved.

Delivery evidence is recorded separately from objective verification. Without
a task-specific objective verifier, the canonical Work/Run remain waiting for
verification and the control plane does not write `verified=True`, a passed
objective-verification record, or a completed Work. A future task type may
register a stronger verifier with explicit acceptance criteria and evidence
scope; only that verifier may establish objective completion.

Alert-level policy: each alert fingerprint can be set individually to `auto` (auto-fix, default), `manual` (wait for your decision after the alert; `/cp run` executes or `/cp ignore` ignores), or `ignore` (ignore directly). Alertmanager's `resolved` is only an observation: the control plane must complete deterministic recovery verification through the current PromQL, HTTP probe, or container state before it interrupts an in-progress repair and resets that fingerprint's auto-fix attempt count. Without a verifier, or when verification fails, the attempt count is kept.

Precipitated files (openable directly):

- `D:\agent\control-plane\data\agent-sessions\{repair_id}.jsonl` (codex exec transcript; `-last.md` is no longer generated)
- `D:\agent\control-plane\data\evidence\` (EvidenceRecord JSON)
- `D:\agent\control-plane\data\patches\` (candidate patches)
- `D:\agent\control-plane\data\control-plane.db` (repairs/actions/candidates/playbooks)

You can also view recent evidence with `/cp evidence` or `GET /v1/evidence` (requires X-Control-Plane-Key).

## Interaction experience

The repair lifecycle is notified in stages through Feishu, reducing "silent waiting":

- Repair starts: repair_id, alert description, today's remaining agent budget; known patterns show how many times they have been supported.
- Agent started: target repository / branch / model.
- Verification passed → repair complete (verification summary + rollback command); failure explains the reason and next steps. If the agent times out but has committed a candidate, it moves to pending approval, not dropped as failed.
- Noise tiers: test/smoke alerts (AlertmanagerE2E, smoke-* etc.) are only recorded and do not trigger repairs; cooldown-skip shows the remaining time; a recurring candidate shows "known pattern, occurrence N".
- Alert recovery: only after deterministic recovery verification passes does it interrupt an in-progress repair and reset attempts; receiving `resolved` alone is not enough to prove recovery. Recovered alerts do not precipitate candidate experience.
- Approval: after a code change, wait for `/cp approve|reject|rollback`, or call `POST /v1/approvals/{repair_id}/decision` directly (requires X-Control-Plane-Key).
- Observability: `/metrics` exposes repair counts/status, candidates, budget, and today's agent calls; `control_plane_run_info`, `control_plane_health_last_ready`, and `recovery_retry_failed` metrics; batch 5 added `control_plane_model_connectivity{source}` (three-source connectivity: Codex CLI / model gateway / default model), `control_plane_model_drift` (model-list drift), `control_plane_ignored_errors_total{site}` (controlled-ignore counts), `control_plane_repairs_recoverable{status}` (recoverable quiescent repairs), and `control_plane_codex_cli_info{version,path}`; batch 6 (2026-08-16) added `control_plane_last_scan_ts` / `control_plane_last_digest_ts` (success heartbeats of the daily scan/digest loops, consumed by `ControlPlaneDailyScanStale` / `ControlPlaneDailyDigestStale`) and `control_plane_notify_failures_total{reason}` (outbound Feishu notification failures, consumed by `ControlPlaneNotifyFailed`); `/live` and `/ready` let probes distinguish liveness from readiness, and are wired into Prometheus and the Metratio Overview dashboard.

Related config: `notify_cooldown_skip`, `notify_ignored_noise`, `test_alert_alertnames`, `test_alert_instance_prefixes`. Batch 2 new config (run_id/PID, `[timeouts]`, `[candidates]`, dirty policy, audit cap, SSH443, side-effect gate, path blacklist) is described below in "Reliability (batch 2)".

## Deployment

```powershell
# Run in an elevated PowerShell 7
./scripts/install-control-plane.ps1
```

The script registers the boot task `ControlPlane` and the per-minute probe task `ControlPlaneWatchdog`: the main task runs the PowerShell supervisor (`Run-ControlPlane.ps1`) through `wscript.exe //B //NoLogo` + `scripts/Run-ControlPlaneHidden.vbs` with a synchronous hidden wrapper; task state matches the supervisor lifecycle; the watchdog does a read-only `/live` check then exits when healthy, and only starts the main task when it is not running and the liveness probe fails. A child-process exit or 4 consecutive `/live` failures makes the supervisor terminate and hand over to the task restart. `data/logs/control-plane.launcher.log[.1-.5]` records only time, PID, exit code, HTTP status, and exception type; service stdout/stderr go to rotation logs in the same directory and keep application-layer redaction, never logging keys. The log directory is accessible only to the current user, SYSTEM, and Administrators. The install also enables the `Microsoft-Windows-TaskScheduler/Operational` history and adds a firewall rule limited to the local subnet (TCP 18083). The key is provided through the user environment variable `CONTROL_PLANE_API_KEY`; it never enters the repository, logs, or command history. Alertmanager reads the same shared key from a read-only secret volume and sends it via `Authorization: Bearer`; the alert entry accepts no query-parameter key, and other control APIs keep using `X-Control-Plane-Key`.

Both the main task and the watchdog use the `wscript.exe //B //NoLogo` hidden wrapper (main task `scripts/Run-ControlPlaneHidden.vbs`, watchdog `scripts/Watch-ControlPlaneHidden.vbs`) to avoid a bare pwsh console creating a host window under the Windows Terminal default terminal; the wrapper VBS waits synchronously with `shell.Run(..., True)` and passes the exit code through with `WScript.Quit exitCode`, so task state, supervision, rotation logs, and exit evidence match bare pwsh (see ADR-0011 revision record).

## Candidate experience lifecycle

- A successful fix automatically creates a candidate (90-day term, archived by default on expiry).
- Candidates only enter the Agent's reasoning context; they do not automatically gain modification permission.
- `/cp promote <candidate_id>` requires Feishu approval; after promotion it enters the official playbook and participates in automated tool decisions.

## Agent operation boundary

The Codex agent may run read-only diagnostics, URL probes and PromQL queries. It may not execute `docker restart`, `docker compose restart/up -d`, `git merge` or `git push`. Those effects are performed only by the private typed Git/Docker providers after a scoped `AuthorizationGrant`; the provider allowlists Compose projects and rejects force-push, destructive volume/database operations, credential/firewall/sshd changes, and stopping or deleting containers holding persisted data. For Docker, `desired_state_verified` means the current containers satisfy the running/healthy postcondition. `event_attribution` remains `unknown` for `docker.restart` unless an independent event observer proves the restart, and is `not-applicable` for `docker.compose.up`; healthy containers must not be used as restart-event evidence.

## Verifier (deterministic checks)

After a repair completes, a deterministic module verifies — model self-reports are not accepted:

- Containers: `docker ps` state must be `Up`; `unhealthy` / `restarting` is a failure; no container is a failure.
- Probes: the HTTP status code must be in the expected set (default 200/301/302/307/401); response-body keywords can be checked.
- PromQL: the query must have results; when `expected` is specified, all sample values must equal the expected value.
- Logs: scan a time window (default last 10 minutes, 200 lines) for fatal patterns (Traceback/panic/FATAL etc., configurable).
- Git: diff verification only when the agent actually changed code (branch recorded); non-git directories skip the diff.

Evidence is derived automatically by alert type: `ServiceDown` (instance is a URL) → probe + `probe_success == 1`; `PrometheusScrapeFailed` → `up{instance=...} == 1`; pure-ops repairs with no code/probe evidence fall back to the allowlisted project container baseline. With no deterministic check at all, it is judged a failure (minimum_evidence).

Recovery verification uses the same class of deterministic evidence: dev-environment alerts require `dev_environment_health == 1` and non-stale maintenance metrics; stale-metric alerts require the newest sample to re-enter the 7-hour window; service-unreachable uses HTTP probes in the allowlist; scrape failures use `up == 1`; known-project alerts use container health state. When running npm audits, always explicitly use `--registry=https://registry.npmjs.org`.

## Daily precipitation cleanup

The control plane automatically runs one precipitation cleanup daily at 21:30 (`POST /v1/digest` can trigger manually):

- Uses a model to review candidate experience, recent evidence files, and session summaries;
- `DROP` candidates are archived automatically, `KEEP` candidates are retained;
- One Feishu message is sent regardless of outcome: retained items are listed with a prompt to `/cp promote <id>` or `/cp dismiss <id>`; when nothing is precipitated or it has no value, a short confirmation is sent.

Config: `digest_enabled`, `digest_time`, `digest_max_candidates`.

## Daily environment self-check

The control plane automatically runs one environment self-check daily at 06:00 (`POST /v1/scan` can trigger manually):

- Local: free space on C:/D drives, Docker containers unhealthy/restarting, Prometheus readiness and firing alerts;
- Cloud (via `ssh metratio`): disk usage, certificate days-to-expiry, webhook-relay state, SSH firewall rules (should allow only Tailscale sources), gateway-nginx running state, pending security-update count;
- Silent when no differences (log only); pushes one Feishu summary when there are differences.

Config: `scan_enabled`, `scan_time`, `scan_disk_free_gb_min`, `scan_cloud_free_gb_min`, `scan_cert_days_warn`.

The control plane listens on `127.0.0.1:18083` by default (`[server] host` in `control_plane.toml`); Docker containers reach it via `host.docker.internal`; it is not exposed to the LAN.

## Reliability (batch 2)

### Stable run ID and single instance

- Each startup generates a `run_id` (timestamp + random), written to the `run_records` table, the first log line, and the evidence file header (`evidence_header.run_id`).
- After a new instance acquires the single-instance lock, it converges any `run_records.status=running` from a previous run that did not close normally to `interrupted` and records the stop time, so a hard termination leaves no false running state.
- Startup writes `data/control-plane.pid`; before restart it checks whether the old PID is alive and refuses to start if so, avoiding double instances (dual python processes happened historically). Graceful stop completes SQLite writes, cancels agent tasks, cleans the PID file, and records stopped.
- Timeout/cancel uniformly terminates the process tree: `taskkill /PID <pid> /T /F`, solving "scheduled tasks cannot fully terminate Python child processes"; `assert_no_residual_processes` verifies no residual git/python/node/ssh child processes.

### Timeout classification

`[timeouts]` distinguishes four kinds and records each separately (repairs.timeout_kind + evidence):

- `exec_seconds`: agent run timeout → `TIMED_OUT` when no commit, otherwise stays pending review;
- `comm_seconds`: alertmanager/feishu/git/ssh network timeout → retryable;
- `verify_seconds`: verifier timeout → retryable;
- `approval_seconds`: waiting for human approval times out (effective when >0) → `ESCALATED`, no automatic retry.

### Restart recovery

- `needs_approval` candidates automatically resume approval waiting after restart: on startup, waiters for `/cp approve|reject|rollback` are registered and re-notified; candidates committed before restart can continue approval (with evidence summary: commit hash, diff stat, touched files, impact description).
- Failure recovery keeps the dual evidence chain of `original_error` and `recovery_error`, and exposes the `recovery_retry_failed` metric label (failed again after recovery).
- Attempt counts are zeroed only after deterministic recovery verification succeeds (`resolved` alone is not enough to reset).
- Startup alert reconciliation: on startup `reconcile_alerts()` uses the live alert source to resolve webhook `resolved` events that may have been lost during the restart window, sharing `_handle_resolved` with the webhook path, avoiding stale firing rows (2026-08-14).

### Git branch safety

- Before restoring the original branch, the worktree is checked; when dirty, restoration is **abandoned** and the error is recorded — user's uncommitted changes are never overwritten.
- Local Git scaffolding (candidate worktree add/remove, branch restoration, and stale-candidate deletion) runs through the private `GitPhysicalBoundary`. Repository containment, branch identity, worktree occupancy, and cleanliness are rechecked at the mutation point; an unknown check fails closed and never becomes "clean".
- `dirty_worktree_policy`: `reject` (default, refuse execution on a dirty worktree) | `isolate` (legacy preflight mode; candidate `workspace-write` sessions are isolated by `[agent] isolate_worktree` regardless).
- `[candidates].branch_prefix` unifies candidate branch naming (internal field `candidate_branch_prefix`, default `fix/control-plane-`).

### Candidate branch cleanup

- Candidate cleanup is advisory-only: `uv run python -m control_plane cleanup-candidates` (and `POST /v1/candidates/cleanup`) enumerates merged / rejected (repair is rejected/rolled_back) / expired (older than `[candidates].retention_days`, internal field `candidate_retention_days`) branches.  `--apply` and `{"apply": true}` are retained for compatibility but fail closed and never delete; a separate owner-authorized recovery workflow with durable evidence is required.
- `[candidates].cleanup_policy = "auto"` (internal field `candidate_cleanup_policy`) only performs the same startup advisory scan.  It no longer authorizes autonomous deletion; `manual` remains the default.

### Mutual exclusion, classification, and budget

- Concurrent repairs of the same fingerprint are mutually exclusive: in-process `asyncio.Lock` + SQLite persisted lease (`leases` table, `lease_ttl_seconds` controls validity), preventing multiple instances fixing the same alert at once.
- Failure classification `errors.py`: NetworkError/GitConflict → retryable; ValidationError/ConfigError/dirty worktree → deterministic (no more automatic retries); unknown → keep the attempt count.
- Budget follows `budget.py` (daily cap + per-repair cap); cleanup/audit does not consume budget.

### Audit and redaction

- Every command/agent call writes to `command_audit`: redacted args, exit code, duration, truncated, error_class; the redaction function filters token/password/secret/api_key/authorization and similar keys.
- Agent output cap `max_agent_output_bytes` (default 200KB); session JSONL is redacted before truncation, with `truncated=true` recorded.
- Read-only check: `uv run python -m control_plane inspect-sessions` or `GET /v1/sessions/inspect` — lists only the **field names** that may contain sensitive values in sessions, never the values.

### Dependency security advisories

Dependency-update candidates call the GitHub Security Advisories API (urllib, no new dependencies) when generating the pending-review summary and candidate evidence; on network failure it degrades to "Security advisory: unavailable" and marks it in the evidence without blocking approval.

### Health checks

- `GET /live`: 200 when the process is alive (this process only).
- `GET /ready`: SQLite writable (refreshes the `health:last_ready` timestamp), Prometheus and Alertmanager (per config) reachable; 200 only when all pass, otherwise 503 degraded with per-check detail and the last readiness timestamp; `/metrics` also exposes `control_plane_run_info` and `control_plane_health_last_ready`.

### External side-effect gate

- With `external_side_effects_require_approval = true`, runtime-changing repair actions are marked `needs_approval`; approved restarts and Compose lifecycle changes enter the typed Docker provider instead of the Codex process. Docker cache cleanup remains a separate maintenance path and is not implied by the runtime provider contract.
- The path blacklist `blocked_paths` prevents the agent/tools from accessing credentials and sensitive user files; any candidate diff touching `.env`/credentials/secrets/id_rsa/`.pem`/`.key`/token/password and similar paths is always refused for merge.
- `data/protection-*.json` are GitHub branch-protection rule snapshots (inspection targets), not an allowlist for the scripts directory; scripts/ actually contains only the install script.

### GitHub SSH 443 fallback

- The typed `git.push` provider prefers SSH port 22; network-class failures automatically fall back to `ssh.github.com:443` (`github_ssh_host_port` configurable), injecting a restricted `GIT_SSH_COMMAND` (BatchMode + ConnectTimeout + accept-new); two failures throw a classified error.

## Hardening (batch 5)

### Codex executable path and preflight

- Resolution priority: `[agent] codex_cli` > PATH `codex` (scoop shim) > bare `codex`.
- Before every session, `codex --version` preflight; missing/failed is rejected clearly with `CodexCliUnavailableError`; version changes are recorded to `codex:cli_version` and alerted.

### State semantics (terminal vs recoverable quiescent)

- `TERMINAL_STATES` (unrecoverable): `closed`/`rolled_back`/`failed`/`escalated`/`timed_out`, all with no outgoing edges; `TIMED_OUT` is used only for exec timeout with no candidate commit (decided by `_failure_status_for`).
- `RECOVERABLE_STATES` (quiescent recoverable): `interrupted`/`recovering`/`needs_approval`. `interrupted` is no longer terminal: fingerprint cooldown dedup no longer treats interruption as completion, the `repairs_active` metric excludes all quiescent states, and `control_plane_repairs_recoverable{status}` is exposed separately.
- Timeouts that produced a commit go through `needs_approval` (recoverable), not `TIMED_OUT`.

### Error-body redaction and Responses API semantics

- Upstream error bodies of the Responses-compatible client are first redacted field-level (sensitive keys) and scanned for inline key values (`api_key`/`token`/`secret` etc. followed by a long value); `response.text[:500]` is never carried verbatim into exceptions or logs.
- Parsing explicitly handles refusal (top-level/output-item/content fragments), treats non-`completed` `status` as `incomplete`, and collects unknown output types into `unknown_output_types`; function-call argument parse failures keep the `arguments_parse_error` reason instead of being silently dropped.

### Model-gateway diagnostic client network boundary

- `gateway_base_url` (default `http://127.0.0.1:4101/v1`) is the probe target for model-source diagnosis (LiteLLM `GET /v1/models`), not a routing config for `codex exec`. Only loopback is supported; non-loopback addresses are rejected at startup (ConfigurationError). Every request carries `X-Request-Id` for tracing, and exception messages include the request id. The old `opencodex_base_url`/`opencodex_api_key` fields are no longer read.

### Model-source connectivity and drift

- `check_model_sources()` runs a minimal connectivity regression over three sources: the Codex CLI (`--version`), the local model gateway (LiteLLM 4101, `GET /v1/models`), and the default model (whether `config.model` is in the list); results are published as `control_plane_model_connectivity{source}` (source=cli/gateway/model).
- `check_model_drift()` compares the model list with the `models:baseline` baseline read-only; on drift it updates the baseline and notifies (`control_plane_model_drift`).
- Startup preflight `startup_model_preflight()` (`model_preflight_enabled` default on): a missing default model or unreachable gateway sends a warning notification ("LiteLLM gateway unreachable") without blocking the service.

### Controlled ignores and resources

- Every `except…pass` is audited per site: genuinely ignored ones add a debug reason; critical paths (audit write failure, process-tree termination fallback, repo restore/enumeration, probe construction, dirty check, disk-usage parsing, docker df parsing) count into `control_plane_ignored_errors_total` by `site`.
- All httpx clients unify the connection-pool cap (`max_connections=20`, `max_keepalive_connections=10`); service/ToolContext/`GatewayClient` close only their own clients; cancelled requests are returned to the pool (tested).

### Static typing and coverage gates

- `uv run mypy src/control_plane` (pyproject `[tool.mypy]`, src scope) baseline passes and is in CI.
- Coverage baseline `scripts/coverage-baseline.txt` (75%, CI portable-suite scope); `uv run python scripts/check_coverage.py` acts as a decline gate (tolerance 0.5%) and is in CI.

### Database migration

- SQLite migrations (`_ensure_column` etc.) are covered by tests: old-schema upgrades add columns, repeated initialization is idempotent, existing rows are kept, partial new-column scenarios, and "a new database can be read and written by old-version code" (downgrade compatibility).

### ADR

- [0002 process-tree kill and PID](/docs/decisions/0002-process-tree-kill-and-pid.md)
- [0003 timeout classification](/docs/decisions/0003-timeout-classification.md)
- [0004 branch restore and dirty policy](/docs/decisions/0004-branch-restore-and-dirty-policy.md)
- [0005 audit and redaction](/docs/decisions/0005-audit-and-redaction.md)
- [0006 SSH443 fallback](/docs/decisions/0006-ssh443-fallback.md)
- [0007 least-privilege run account](/docs/decisions/0007-least-privilege-run-account.md)
- [0008 scheduled task to Windows service (research)](/docs/research/0008-scheduled-task-to-windows-service.md)
- [0009 upgrade vs fix authorization](/docs/decisions/0009-upgrade-vs-fix-authorization.md)
- [0010 OpenCodex network boundary and model source](/docs/decisions/0010-opencodex-network-boundary.md)
- [0012 model-gateway connectivity diagnosis (after OpenCodex retirement)](/docs/decisions/0012-model-gateway-connectivity.md)


