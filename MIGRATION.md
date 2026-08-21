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
public source tree is read-only from this project. A private semantic hardening
delta must be documented, tested, and treated as an upstream follow-up; it
must not be mistaken for a new private authority. The current private delta
includes capability-scoped Codex sandbox selection and the monotonic procedure
profile resolver. Do not claim zero drift until the corresponding public
changes have landed and a new pin has been recorded.

The private repository currently builds both packages so the personal profile
can run from one checkout:

```toml
[tool.hatch.build.targets.wheel]
packages = ["src/control_plane", "src/portable_runtime"]
```

This is packaging convenience, not ownership transfer. Do not replace it with
a thin-shim or archived `control_plane` target without a separately approved
architecture decision.

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
6. Provider execution success is an execution outcome, not repair verification;
   Work/Run remain waiting until deterministic verification finalizes them.
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
- public-base cleanliness and pin verification;
- production `/live` and `/ready` after launcher changes.

## Historical notes

The earlier extraction wording that described `control_plane` as deprecated or
archived is obsolete. It has been replaced by this two-project boundary. The
canonical authority cutover and the private entrypoint correction are recorded
in `docs/decisions/0015-personal-runtime-authority-cutover.md` and
`docs/refactor/progress.md`.
