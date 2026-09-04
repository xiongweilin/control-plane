# control-plane

Personal deployment/profile for [agent-kernel](https://github.com/xiongweilin/agent-kernel).

This repository no longer contains a runtime implementation. Generic cognitive
control, durable Work/Run execution, persistent responsibility, records,
authorization, recovery, verification and provider routing are imported from
`agent-kernel` through its compatibility Python distribution name
`portable-runtime` / namespace `portable_runtime`.

## Boundary

`control-plane` owns only personal/platform-specific concerns:

- Windows launch/watchdog/Task Scheduler integration;
- personal Codex process isolation and credential/Docker denial boundary;
- local LiteLLM/Codex configuration;
- Alertmanager/Prometheus ingress and readiness checks;
- Feishu human/notification providers selected from Agent Kernel;
- personal Git/Docker effect provider with local project/repository allowlists;
- CS2 game-mode alert suppression;
- personal API authentication and thin HTTP ingress.

Everything else is Agent Kernel.

```text
Alertmanager / user request / Windows profile
                |
                v
          control_plane
     profile config + adapters
                |
                v
          agent-kernel
 CognitiveController / Runtime
 Responsibility / Records / Authority
 Recovery / Verification / Providers
                |
                v
       model / tools / reality
```

## Deliberately absent

The following are intentionally deleted and must not return:

```text
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

There is no compatibility projection. If Agent Kernel needs a semantic feature,
it is added upstream and consumed as a dependency rather than copied here.

## Agent Kernel dependency

`pyproject.toml` pins the `portable-runtime` distribution directly to the
Agent Kernel K3 commit. The repository/product name is `agent-kernel`; the
package/namespace remain compatibility axes owned upstream.

## Personal HTTP surface

- `GET /healthz` — process health.
- `GET /live` — liveness.
- `GET /ready` — Prometheus/Alertmanager readiness plus provider health.
- `GET /metrics` — Prometheus metrics.
- `GET /v1/runtime` — Agent Kernel runtime/work/contract view (API key).
- `POST /v1/tasks` — creates Work and a durable CognitiveController read-only
  reasoning step.
- `POST /v1/alerts/alertmanager` — authenticated Alertmanager ingress; firing
  alerts become Agent Kernel cognitive work unless game-mode suppression applies.
- `GET /v1/game-mode` — current personal game-mode projection.
- `GET /v1/sessions/inspect` — reports sensitive field names only, never values.

Git/Docker side-effect capabilities are registered as personal providers, but
Agent Kernel marks them authorization-required. The profile does not mint
execution authority merely because an API request or model result exists.

## Setup

```powershell
$env:CONTROL_PLANE_API_KEY = "..."
uv sync --extra dev
uv run control-plane
```

Example configuration is `control_plane.toml.example`.

Windows deployment scripts live only under
`deployments/windows-personal-platform/`.

## Verification

```powershell
uv sync --extra dev
uv run ruff check .
uv run mypy src
uv run pytest -q
```

A structural test fails if an embedded `src/portable_runtime` tree or any of the
retired generic control-plane modules reappears.
