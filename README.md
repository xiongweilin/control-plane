# control-plane

Autonomous personal operations deployment/profile for [agent-kernel](https://github.com/xiongweilin/agent-kernel).

`control-plane` has no GUI. It normally runs unattended. Personal commands can still enter through the existing Feishu gateway or the authenticated HTTP task surface; all cognitive/model work is dispatched through Agent Kernel to Codex.

This repository contains no runtime implementation. Generic cognitive control, durable Work/Run execution, persistent responsibility, records, authorization, recovery, verification and provider routing are imported from `agent-kernel` through its compatibility Python distribution name `portable-runtime` / namespace `portable_runtime`.

## Operating model

```text
PC / Prometheus / Alertmanager
            |
            v
      control-plane
 profile facts + ingress + physical boundaries
            |
            v
       agent-kernel
 ControllerPolicy / CognitiveController
 Runtime / Work / Records / Authority
 Recovery / Verification / Providers
            |
            +------------------------------+
            |                              |
            v                              v
 Codex + gpt-5.6-sol              Prometheus / personal effects
 strong diagnosis                 verification / bounded apply
            |
            v
 Codex + gpt-5.6-luna
 cheap bounded execution
            |
            v
 verify real alert state
      |             |
   solved       unresolved
      |             |
    close       retry once
                    |
             unresolved again
                    |
                  WAIT
                    |
              Feishu notify
                    |
         /cp instruct <id> <command>
                    |
                    v
             new controller cycle
```

Autonomous repair is deliberately bounded to two attempts. Each attempt uses `codex/gpt-5.6-sol` for diagnosis and `codex/gpt-5.6-luna` for execution. If the triggering Prometheus alert is still active after the second attempt, the controller stops and notifies the owner in Feishu. A later explicit Feishu instruction starts a follow-up controller on the same Agent Kernel Work.

## Boundary

`control-plane` owns only personal/platform-specific concerns:

- Windows launch/watchdog/Task Scheduler integration;
- personal Codex process and credential/Docker boundary;
- local LiteLLM/Codex model configuration;
- Alertmanager/Prometheus ingress and read-only verification provider;
- the personal two-attempt repair `ControllerPolicy`;
- Feishu task/command ingress compatibility and notification providers;
- personal Git/Docker effect provider with local project/repository allowlists;
- CS2 game-mode alert suppression;
- personal API authentication and thin administrative HTTP ingress.

Everything else is Agent Kernel.

For configured `allowed_auto` projects, a clean exact project repository is standing local auto-repair scope: Codex may edit it in place through the Kernel `shell.exec` capability. Docker and remote Git credentials remain physically denied to Codex. Applying the configured Compose project is a separate Agent Kernel capability (`docker.compose.up`) and the personal provider independently re-checks the project allowlist. Remote Git changes, rollback and targeted restart remain authorization-required capabilities.

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
local state machine / verifier / evidence plane
vendored upstream tests and migration scripts
portable-local deployment
```

There is no compatibility projection. If Agent Kernel needs a semantic feature, it is added upstream and consumed as a dependency rather than copied here.

## Agent Kernel dependency

`pyproject.toml` pins the `portable-runtime` distribution directly to the Agent Kernel commit that includes the stable `ControllerPolicy` seam and strict reopen guards. The repository/product name is `agent-kernel`; the package/namespace remain compatibility axes owned upstream.

## Personal HTTP surface

- `GET /healthz` — process health.
- `GET /live` — liveness.
- `GET /ready` — Prometheus/Alertmanager readiness plus provider health.
- `GET /metrics` — Prometheus metrics.
- `GET /status` — concise personal runtime/model status for Feishu `/cp status`.
- `GET /v1/runtime` — Agent Kernel runtime/work/contract view (API key).
- `POST /v1/tasks` — explicit personal command/task; strong-model interpretation followed by cheap-model execution through Agent Kernel.
- `POST /v1/alerts/alertmanager` — authenticated unattended incident ingress; two autonomous repair attempts maximum.
- `POST /v1/controllers/{controller_id}/command` — explicit owner instruction for a controller that stopped in `WAITING`; creates a follow-up controller on the same Work.
- `GET /v1/game-mode` — current personal game-mode projection.
- `GET /v1/sessions/inspect` — reports sensitive field names only, never values.

Feishu transport remains owned by `feishu-dify-gateway`. Normal text and `/task` dispatch to `/v1/tasks`; `/cp instruct <controller_id> <command>` dispatches an explicit instruction to a waiting repair.

## Models

```toml
[model]
diagnosis_model = "codex/gpt-5.6-sol"
execution_model = "codex/gpt-5.6-luna"
```

Both are invoked through the same Agent Kernel `CodexProvider`; the profile selects the per-request model through capability parameters.

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

Structural tests fail if an embedded `src/portable_runtime` tree or any retired generic control-plane module reappears. Tests also assert the strong/cheap model split, restored personal task surface, bounded two-attempt repair policy and Feishu continuation command boundary.
