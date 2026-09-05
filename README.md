# control-plane

Autonomous personal operations deployment/profile for [agent-kernel](https://github.com/xiongweilin/agent-kernel).

`control-plane` has no GUI. It normally runs unattended. Personal commands still enter through the existing Feishu gateway or authenticated HTTP task surface; Alertmanager ingress, game-mode suppression, repository/project allowlists, notifications and Windows deployment behavior remain personal-profile concerns.

This repository contains no alternative runtime implementation. Generic cognitive control, durable Work/Run execution, persistent responsibility, records, authorization, recovery, verification and provider routing are imported from `agent-kernel` through its compatibility Python distribution name `portable-runtime` / namespace `portable_runtime`.

## Operating model

The personal surfaces are unchanged. The core is now exclusively the Agent Kernel v2 cognitive loop:

```text
PC / Prometheus / Alertmanager / Feishu
                  |
                  v
            control-plane
 ingress + personal facts + physical boundaries
                  |
                  v
             agent-kernel
                  |
                  v
       StandingResponsibility
                  |
                  v
      ResponsibilityAssessment
                  |
                  v
        cognitive exploration
      (read-class capabilities)
                  |
                  v
        CognitiveClosure
                  |
                  v
          WorkProposal
                  |
                  v
 priority / portfolio / reservation / commitment
                  |
                  v
         materialized Work
                  |
                  v
          Work / Run effects
       through Runtime boundary
                  |
                  v
        reality observations
                  |
                  v
       RevisionAssessment
        /       |        \
     close   reopen      wait
               |
               v
       explicit controller REOPEN
               |
               +----> new cognition / closure / proposal
```

There is no controller-to-effect shortcut. A reasoner result is not Work. A diagnosis must first become a `CognitiveClosure`; the closure can only hand off to `WorkProposal`; a proposal must pass the Agent Kernel persistent-responsibility admission/commitment path before Work exists. Execution and personal effects then run against that materialized Work through `Runtime.run_capability`. Reality is returned to cognition as a `RevisionAssessment` before retry, reopen or close.

Autonomous incident repair remains deliberately bounded to two attempts per autonomous pass. Diagnosis and execution still use the configured Luna model, but they now occupy different semantic planes: diagnosis is read-class cognition; execution belongs to materialized Work. If monitoring still reports the triggering alert after the second attempt, the controller waits and notifies the owner. An explicit `/task <controller_id> <command>` does not bypass the failed Work: it records owner direction, creates a new reality-grounded revision, enters `REOPEN_REQUIRED`, and then explicitly reopens cognition on the same controller history.

## Boundary

`control-plane` owns only personal/platform-specific concerns:

- Windows launch/watchdog/Task Scheduler integration;
- personal Codex process and credential/Docker boundary;
- local LiteLLM/Codex model configuration;
- Alertmanager/Prometheus ingress and read-only verification provider;
- the personal bounded repair and manual-task `ControllerPolicy` implementations;
- the thin mapping from personal task context into Agent Kernel standing responsibility/admission objects;
- Feishu task/command ingress compatibility and notification providers;
- personal Git/Docker effect provider with local project/repository allowlists;
- CS2 game-mode alert suppression;
- personal API authentication and thin administrative HTTP ingress.

Everything else is Agent Kernel.

For configured `allowed_auto` projects, a clean exact project repository remains standing local auto-repair scope. Codex may edit it through the Kernel `shell.exec` capability. Docker and remote Git credentials remain physically denied to Codex. Applying the configured Compose project remains a separate Agent Kernel capability (`docker.compose.up`) and the personal provider independently re-checks the project allowlist. Remote Git changes, rollback and targeted restart remain authorization-required capabilities.

Health/readiness, game-mode projection and session-field inspection are deployment probes or ingress facts, not Agent Work. Model calls, alert verification, notifications and repair/effect execution cross Agent Kernel provider/capability boundaries.

## Deliberately absent

The following are intentionally absent and must not return:

```text
GUI
src/portable_runtime/
portable-runtime-pin.json
legacy Store / repair DB
RepairService
PortableRuntimeAuthority
ClosureAuthority
ReconciliationDescriptorStore
local controller state machine
local Work admission kernel
local verifier / evidence plane
vendored upstream tests and migration scripts
portable-local deployment
```

There is no dual core or legacy fallback. If Agent Kernel needs a semantic feature, it is added upstream and consumed as a dependency rather than copied here.

## Agent Kernel dependency

`pyproject.toml` pins `portable-runtime` directly to the Agent Kernel commit containing cognitive-control v2: explicit `CognitiveClosure`, closure-bound `WorkProposal`, persistent-responsibility admission/commitment, reality-grounded `RevisionAssessment`, and strict reopen guards. The repository/product name is `agent-kernel`; the package/namespace remain compatibility axes owned upstream.

## Personal HTTP surface

The external personal surface is preserved:

- `GET /healthz` — process health.
- `GET /live` — liveness.
- `GET /ready` — Prometheus/Alertmanager readiness plus provider health.
- `GET /metrics` — Prometheus metrics.
- `GET /status` — concise personal runtime/model status for Feishu `/cp status`.
- `GET /v1/runtime` — Agent Kernel runtime/work/contract view (API key).
- `POST /v1/tasks` — explicit personal command/task. A prompt beginning with `<controller_id> ` remains an explicit continuation command for that waiting controller.
- `POST /v1/alerts/alertmanager` — authenticated unattended incident ingress; two autonomous repair attempts maximum.
- `POST /v1/controllers/{controller_id}/command` — direct authenticated equivalent of the Feishu continuation form.
- `GET /v1/game-mode` — current personal game-mode projection.
- `GET /v1/sessions/inspect` — reports sensitive field names only, never values.

Environment inspection, metric names, fail-safe alert routing and manual escalation boundaries
are documented in [`docs/environment-checks.md`](docs/environment-checks.md).

Feishu transport remains owned by `feishu-dify-gateway`. Normal text and `/task` continue to dispatch to `/v1/tasks`, so no second Feishu command protocol is introduced. Escalation still tells the owner to send `/task <controller_id> <explicit command>`.

## Models

```toml
[model]
diagnosis_model = "gpt-5.6-luna"
execution_model = "gpt-5.6-luna"
gateway_base_url = "http://127.0.0.1:4101/v1"
```

The Codex CLI receives the same official model through `http://127.0.0.1:4100/v1`; the 4100 proxy forwards to LiteLLM on 4101. The model list is generated from the official Codex cache by `D:\agent\litellm-gateway\scripts\sync-agent-gpt-models.ps1` without creating a filtered Codex catalog.

Both phases use the same Agent Kernel `CodexProvider`; the semantic distinction is enforced by the core loop and capability boundary rather than by a separate model tier.

## Setup

```powershell
$env:CONTROL_PLANE_API_KEY = "..."
uv sync --extra dev
uv run control-plane
```

Example configuration is `control_plane.toml.example`.

Windows deployment scripts live only under `deployments/windows-personal-platform/`.

## Verification

```powershell
uv sync --extra dev
uv run ruff check .
uv run mypy src
uv run pytest -q
```

Structural tests fail if an embedded `src/portable_runtime` tree or any retired generic control-plane module reappears. Cognitive-loop tests fail if execution is reachable directly from diagnosis: they require closure, proposal, materialized Work lineage and revision before close/reopen. Route tests lock the existing personal HTTP surface in place.
