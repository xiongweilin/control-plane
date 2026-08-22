# Migration: portable-runtime base and private control-plane profile

## Current goal

This is a deliberate two-project architecture, not a plan to archive
`control-plane`:

- Public `ratiolin/portable-runtime` owns the provider-neutral Work/Run,
  capability, authorization, reliability and `RealityBoundary` semantics.
- Private `ratiolin/control-plane` vendors that runtime as its base and adds
  the personal Windows, Feishu, Prometheus, Alertmanager, Codex and legacy HTTP
  integration surface.
- `python -m control_plane` remains the private production entrypoint.

The public project must remain independently usable and must not import private
modules, secrets, metratio URLs or `D:\agent` paths. The private profile may
depend on `portable_runtime`; the dependency direction does not reverse.

## Ownership rules

Portable Runtime owns:

- Work/Run/record and canonical state transitions;
- capability effect contracts and minimum procedure profiles;
- authorization, qualification, reliability, fencing and RealityBoundary;
- provider protocol and capability-scoped Codex execution semantics.

The private Windows profile additionally owns the deployment-specific Codex
process boundary: detached candidate worktrees and host-control credential
scrubbing are implemented by the private `control_plane.codex_runner` helper
and injected into the actual vendored Codex provider at app bootstrap, while
the public provider remains provider-neutral. This boundary must not be used as
a reason to add personal paths, secrets or Docker policy to the public
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

`src/portable_runtime` is synchronized from the public repository and the
public source tree is read-only from this project. The provider-neutral
semantic hardening delta is pinned to public commit `f20cb87` and the private
vendored tree is checked against that exact commit in CI. The machine-readable
`portable-runtime-pin.json` records the commit and normalized provider-neutral
tree digest; `scripts/verify_portable_runtime_pin.py` rejects stale, missing,
extra, or modified vendored files. The remaining private provider difference
is deployment-specific Windows boundary injection and is
not a second semantic authority.

The private repository currently builds both packages so the personal profile
can run from one checkout:

```toml
[tool.hatch.build.targets.wheel]
packages = ["src/control_plane", "src/portable_runtime"]
```

This is packaging convenience, not ownership transfer. Do not replace it with
a thin-shim or archived `control_plane` target without a separately approved
architecture decision.

## Upstream synchronization status

The provider-neutral procedure floors, explicit capability contracts,
qualification Decision lookup, typed store access, and Codex sandbox mapping
are now present in public `ratiolin/portable-runtime` at `f20cb87`. The private
profile intentionally retains only the detached worktree and Windows
credential/Docker environment boundary, which depends on this profile's
filesystem and deployment policy and must not be moved into the public base.
That boundary is now injected through the public provider's
`ExecutionBoundary` protocol; `src/portable_runtime/providers/codex/provider.py`
is byte-identical to the public provider, while
`control_plane.codex_boundary.CodexExecutionBoundaryAdapter` supplies the
private Windows implementation.

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
- public-base cleanliness and pin verification (`portable-runtime-pin.json`;
  CI fetches the exact public commit before running the verifier);
- production `/live` and `/ready` after launcher changes.

## Repository governance

The source workflows define the checks that must be required on protected
`main` branches. GitHub repository settings remain the authoritative owner of
that policy and must require, at minimum:

- `portable-runtime`: `CI / lint-and-test` and `CI / strict-conformance`;
- `control-plane`: `CI / lint-and-test` and `CI / windows-native`.

The SonarCloud job remains an additional main-branch quality signal. A local
green workflow is not treated as branch protection; changes to required checks
must be made in the repository settings and then verified with the GitHub API.

## Historical notes

The earlier extraction wording that described `control_plane` as deprecated or
archived is obsolete. It has been replaced by this two-project boundary. The
canonical authority cutover and the private entrypoint correction are recorded
in `docs/decisions/0015-personal-runtime-authority-cutover.md` and
`docs/refactor/progress.md`.
