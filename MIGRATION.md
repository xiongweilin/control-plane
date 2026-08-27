# Migration: portable-runtime base and control-plane profile

## Current goal

This is a deliberate two-project architecture, not a plan to archive
`control-plane`:

- `xiongweilin/portable-runtime` owns the provider-neutral Work/Run,
  capability, authorization, reliability and `RealityBoundary` product semantics.
- `xiongweilin/control-plane` vendors that runtime as its base and adds
  the personal Windows, Feishu, Prometheus, Alertmanager, Codex and legacy HTTP
  integration surface.
- `python -m control_plane` remains the personal-platform production entrypoint.

The upstream project must remain independently usable and must not import
profile-specific modules, secrets, metratio URLs or `D:\agent` paths. The
profile may depend on `portable_runtime`; the dependency direction does not
reverse.

## Ownership rules

Portable Runtime owns:

- Work/Run/record and canonical product state transitions;
- capability effect contracts and minimum procedure profiles;
- authorization, qualification, reliability, fencing and RealityBoundary;
- provider protocol and capability-scoped Codex execution semantics.

The Windows profile additionally owns the deployment-specific Codex process
boundary: detached candidate worktrees and host-control credential scrubbing
are implemented by `control_plane.codex_runner` / the profile boundary adapter
and injected into the vendored Codex provider at app bootstrap, while the
upstream provider remains provider-neutral. This boundary must not be used as
a reason to add personal paths, secrets or Docker policy to the upstream
repository.

Control Plane owns:

- Alertmanager/Feishu/Prometheus and Windows deployment adapters;
- personal owner policy, allowed repositories, budgets and local paths;
- the legacy HTTP/CLI/SQLite compatibility projection;
- the `control_plane.__main__` launcher and scheduled-task glue.

Legacy tables and routes remain compatibility projections. New personal repair
traffic materialises canonical Work/Run first and enters the portable
`RealityBoundary`; `_LegacyRoutingBoundary` is retained only for explicitly
constructed compatibility callers.

## Vendored runtime policy

`src/portable_runtime` is synchronized from the upstream repository and the
upstream source tree is read-only from this project.

The sole authoritative synchronization pin is:

```text
portable-runtime-pin.json
```

That file records the exact upstream commit, synchronized scope and normalized
tree digest. Documentation must not duplicate its commit SHA as a second pin.
`scripts/verify_portable_runtime_pin.py` rejects stale, missing, extra or
modified vendored files against that machine-readable pin.

A newer `portable-runtime` main commit is not, by itself, authorization to
advance this profile's vendored base. Pin advancement is an explicit
synchronization change that must update the vendored tree, machine pin and
verification evidence together.

The remaining profile-specific provider difference is deployment-specific
Windows boundary injection and is not a second semantic authority.

The repository currently builds both packages so the personal profile can run
from one checkout:

```toml
[tool.hatch.build.targets.wheel]
packages = ["src/control_plane", "src/portable_runtime"]
```

This is packaging convenience, not ownership transfer. Do not replace it with
a thin-shim or archived `control_plane` target without a separately approved
architecture decision.

## Upstream synchronization status

The exact synchronized upstream revision is read from
`portable-runtime-pin.json`; do not copy the SHA into prose.

The provider-neutral procedure floors, explicit capability contracts,
qualification Decision lookup, typed store access and Codex sandbox mapping
covered by that pin are present in the vendored tree. The profile intentionally
retains only the detached worktree and Windows credential/Docker environment
boundary, which depends on this profile's filesystem and deployment policy and
must not be moved into the provider-neutral base.

That boundary is injected through the public provider's `ExecutionBoundary`
protocol; `src/portable_runtime/providers/codex/provider.py` is expected to
match the pinned upstream provider, while
`control_plane.codex_boundary.CodexExecutionBoundaryAdapter` supplies the
profile-specific Windows implementation.

## Migration invariants

1. Alert admission writes canonical Work/Run before the legacy repair
   projection; projection failure blocks admission.
2. Every local edit is bound to actor, repository resource and git subject
   version, with a typed `AuthorizationGrant`.
3. A capability contract's procedure profile is a floor. Work/Run/request
   metadata may only raise it; unknown profile values fail closed.
4. Independent post-edit verification remains distinct from pre-execution
   qualification and owns repair closure.
5. Git merge/push and Docker lifecycle effects are separate typed providers;
   Codex sessions can propose them but cannot execute them.
6. Provider execution success and artifact delivery are separate from objective
   verification. The default `generic-task` postcondition proves only that a
   run-associated result artifact was delivered; Work/Run remain waiting and
   no `verified=True` or completed Work is written until a task-specific
   objective verifier establishes the requested outcome.
7. Candidate promotion must preserve typed evidence, verification, decision,
   authorization and scope/version records before the legacy official row is
   projected.
8. `D:\agent\portable-runtime` is never modified by a control-plane-only
   migration.

## Gates

- `ruff check src tests`
- `mypy src`
- `scripts/check_portable_core_imports.py`
- full `pytest` suite and coverage regression gate over both packages;
- upstream-base cleanliness and pin verification (`portable-runtime-pin.json`;
  CI fetches the exact pinned commit before running the verifier);
- production `/live` and `/ready` after launcher changes.

## Repository governance

Governance is intentionally asymmetric between the provider-neutral base and
the personal deployment profile:

- `portable-runtime`: `main` must be protected and require `CI / lint-and-test`
  plus `CI / strict-conformance`.
- `control-plane`: profile CI jobs and the vendor verifier are synchronization,
  deployment and experiment evidence; they do not create a second semantic
  authority for portable runtime contracts.

For the upstream base, the SonarCloud job remains an additional main-branch
quality signal. Profile CI and vendor verification remain evidence for safe
synchronization and do not create a second branch-governance standard.

## Historical notes

The earlier extraction wording that described `control_plane` as deprecated or
archived is obsolete. It has been replaced by this two-project boundary. The
canonical authority cutover and the personal entrypoint correction are recorded
in `docs/decisions/0015-personal-runtime-authority-cutover.md` and
`docs/refactor/progress.md`.
