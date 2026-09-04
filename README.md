# control-plane

Unattended personal operations deployment/profile for [agent-kernel](https://github.com/xiongweilin/agent-kernel).

`control-plane` is not an interactive Agent product and has no GUI. Interactive repository development and repair are done with Codex directly. This service exists only for events that occur while no person is actively driving the task: Windows/service health changes, Alertmanager notifications and other personal-platform conditions.

This repository contains no runtime implementation. Generic cognitive control, durable Work/Run execution, persistent responsibility, records, authorization, recovery, verification and provider routing are imported from `agent-kernel` through its compatibility Python distribution name `portable-runtime` / namespace `portable_runtime`.

## Operating model

```text
Interactive development
user -> Codex -> repository

Unattended operations
PC / Prometheus / Alertmanager
            |
            v
      control-plane
 profile facts + ingress + physical boundaries
            |
            v
       agent-kernel
 ControllerPolicy seam / CognitiveController
 Runtime / Work / Records / Authority
 Recovery / Verification / Providers
            |
            v
          Codex
            |
            v
 read-only diagnosis -> notify user -> close
```

The current unattended policy deliberately stops after one read-only diagnosis. It does not automatically edit repositories, merge/push Git changes, restart Docker or claim recovery. If later real operation history shows that candidate repair preparation is repeatedly useful, that policy can be extended without changing Agent Kernel semantics.

## Boundary

`control-plane` owns only personal/platform-specific concerns:

- Windows launch/watchdog/Task Scheduler integration;
- personal Codex process isolation and credential/Docker denial boundary;
- local LiteLLM/Codex configuration;
- Alertmanager/Prometheus ingress and readiness checks;
- one replaceable unattended alert `ControllerPolicy` using Agent Kernel `step()`;
- Feishu human/notification providers selected through Agent Kernel;
- personal Git/Docker effect provider with local project/repository allowlists;
- CS2 game-mode alert suppression;
- personal API authentication and thin administrative HTTP ingress.

Everything else is Agent Kernel.

Health/readiness, game-mode projection and session-field inspection are deployment probes or ingress facts, not Agent Work. Model, notification and side-effect execution cross Agent Kernel provider/capability boundaries.

## Deliberately absent

The following are intentionally absent and must not return:

```text
GUI / chat task surface
generic interactive task endpoint
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
- `GET /v1/runtime` — Agent Kernel runtime/work/contract view (API key).
- `POST /v1/alerts/alertmanager` — authenticated unattended ingress; each firing alert that is not game-mode suppressed is diagnosed through `ControllerPolicy -> CognitiveController.step() -> reason.generate`, then explicitly closed and optionally notified.
- `GET /v1/game-mode` — current personal game-mode projection.
- `GET /v1/sessions/inspect` — reports sensitive field names only, never values.

There is intentionally no generic `/v1/tasks` route. Interactive work should go directly to Codex rather than through the unattended daemon.

Git/Docker side-effect capabilities are registered as personal providers, but Agent Kernel marks them authorization-required. The profile never invokes those effects directly and does not mint execution authority merely because an alert, API request or model result exists.

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

Structural tests fail if an embedded `src/portable_runtime` tree or any retired generic control-plane module reappears. Tests also require the interactive task route to remain absent and the personal alert policy to use Agent Kernel's policy-driven controller seam.
