# control-plane

A concrete [agent-kernel](https://github.com/xiongweilin/agent-kernel) deployment profile for authenticated operations, monitoring, bounded repair, and narrowly scoped external effects.

This repository is intentionally not a second agent runtime.

```text
agent-kernel
= generic durable cognition / responsibility / Work / authorization / recovery semantics

control-plane
= one concrete deployment profile with environment facts, integrations, policies, and effect boundaries
```

The main purpose of this repository is to show where deployment-specific concerns should live without leaking them back into the generic kernel.

## Why this split matters

A reusable runtime should not have to know the details of one machine, one notification channel, one monitoring stack, one project allowlist, or one local deployment policy.

Those are real operational facts, but they are not universal Agent Kernel semantics.

`control-plane` therefore owns the deployment-specific layer while importing generic cognitive control, persistent responsibility, Work/Run execution, records, authorization, recovery, verification, and provider routing from `agent-kernel` through its compatibility Python distribution name `portable-runtime` / namespace `portable_runtime`.

## Example: bounded incident repair

A firing alert does not become an authorized repair merely because a model produced a diagnosis.

The path is:

```text
monitoring signal
        |
        v
control-plane ingress
  authenticate / persist / deduplicate
        |
        v
Agent Kernel cognition
        |
        v
CognitiveClosure
        |
        v
WorkProposal
        |
        v
admission / commitment / authorization
        |
        v
materialized Work / Run
        |
        v
scoped external capability
        |
        v
reality observation / verification
        |
        v
RevisionAssessment
   /        |        \
close     reopen     wait/escalate
```

There is no controller-to-effect shortcut. A reasoner result is not Work. A diagnosis must first become a `CognitiveClosure`; the closure can only hand off to `WorkProposal`; the proposal must pass the Agent Kernel persistent-responsibility admission/commitment path before Work exists. Reality is returned to cognition as a `RevisionAssessment` before retry, reopen, or close.

Autonomous repair is bounded. Provider failure, timeout, or unavailable diagnosis enters `WAIT` without inventing a closure or Work. Irreversible operations or invalid target state escalate rather than silently broadening authority. If verification still reports the triggering condition after the bounded repair budget, the controller waits and notifies the owner.

An explicit owner continuation command does not bypass failed Work. It records new direction, creates a reality-grounded revision, and reopens cognition on the same controller history.

## Deployment boundary

`control-plane` owns only personal/platform-specific concerns:

- platform launch and supervision;
- local model/provider configuration;
- monitoring ingress and read-only verification providers;
- bounded repair and manual-task `ControllerPolicy` implementations;
- mapping personal task context into Agent Kernel standing-responsibility/admission objects;
- command ingress compatibility and notification providers;
- narrowly scoped source-control and deployment effect providers;
- local suppression/maintenance-state policy;
- personal API authentication and thin administrative HTTP ingress.

Everything else belongs upstream in Agent Kernel.

Health/readiness probes and local state inspection are deployment observations or ingress facts, not Agent Work. Model calls, alert verification, notifications, and effect execution cross Agent Kernel provider/capability boundaries.

## Effect safety

Automatic effects are restricted to explicitly configured local scope. Effect providers independently re-check relevant project and state constraints before acting.

Source-control synchronization and deployment are separate capabilities rather than one unrestricted shell boundary. Generic remote changes, rollback, and other consequential operations remain authorization-required.

The important rule is that model selection does not widen runtime authority: capabilities and effect classes remain enforced outside the model.

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

`pyproject.toml` pins `portable-runtime` directly to the Agent Kernel revision used by this deployment profile. The repository/product name is `agent-kernel`; the package/namespace remain compatibility axes owned upstream.

## Public service surface

The profile exposes a small operational surface for:

- health, liveness, readiness, and metrics;
- concise runtime status;
- authenticated task submission and continuation;
- authenticated monitoring ingress;
- controller continuation commands;
- local operational-state inspection.

Transport integrations remain separate from Agent Kernel authority. A transport may forward a request or render a confirmed response, but it does not mint task authority, authorize effects, or decide that an objective has been completed.

## Setup

```powershell
uv sync --extra dev
uv run control-plane
```

Example configuration is `control_plane.toml.example`. Platform-specific deployment scripts live under `deployments/`.

## Verification

```powershell
uv sync --extra dev
uv run ruff check .
uv run mypy src
uv run pytest -q
```

Structural tests fail if an embedded `src/portable_runtime` tree or retired generic control-plane modules reappear. Cognitive-loop tests fail if execution is reachable directly from diagnosis: they require closure, proposal, materialized Work lineage, and revision before close/reopen. Route tests lock the intended public service surface in place.
