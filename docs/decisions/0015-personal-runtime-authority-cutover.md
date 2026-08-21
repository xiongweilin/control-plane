# ADR-0015: Personal Runtime authority cutover

## Status

Accepted

## Date

2026-08-21

## Context

The personal Windows profile had a portable Work/Run store, but the real
repair path still invoked Codex through `_LegacyRoutingBoundary`.  That path
could route a provider without the portable qualification, policy, procedure,
authorization, reliability, precommit and postcondition stages.  Alert ingest
also wrote the legacy repair row first and created an unrelated portable Work
as a best-effort second write.

## Decision

Use one deep adapter, `control_plane.portable_authority.PortableRuntimeAuthority`,
as the seam between the compatibility HTTP/service surface and Portable
Runtime:

```text
Alertmanager / task
  -> canonical Work/Run (stable legacy mapping)
  -> personal-owner policy Decision + AuthorizationGrant
  -> RealityBoundary
  -> CodexProvider(code.edit)
  -> independent legacy verifier
  -> compatibility projection / final status
```

The adapter binds every local edit to:

| Field | Source | Owner | Validation | Allowed use |
| --- | --- | --- | --- | --- |
| `work_id` / `run_id` | legacy repair identity | Portable Store | stable `work_legacy_` / `run_legacy_` mapping | lifecycle and recovery |
| `resource_ref` | resolved repository path | personal policy | exact grant scope | Codex local workspace |
| `subject_version_refs` | `git rev-parse HEAD` | repository | grant/request intersection | this checkout only |
| `actor_ref` | runtime identity | personal policy | matches grant grantee | provider invocation |
| AuthorizationGrant | owner policy | Portable Store | capability, resource, version, effect ceiling, TTL | `code.edit` only |

The personal `code.edit` request keeps the Runtime contract minimum of
`standard`.  Personal authority materialises the typed candidate, evidence,
authorization, source-version verification, rollback/checkpoint and owner
decision records needed by that profile before the provider runs.  The
`RealityBoundary` resolves the effective profile as the maximum of the
contract minimum and all Work/Run/request requirements; metadata can request a
stricter profile but can never downgrade one.  The independent verifier still
owns post-edit verification and repair closure; the pre-execution source
version assertion is not treated as the final repair result.

Runtime-changing effects are separate capabilities. Codex may describe a
recommended merge, push or service restart, but it cannot perform those
effects. The private profile registers `git.merge`, `git.push`, `git.rollback`,
`docker.restart` and `docker.compose.up` providers with scoped contracts and
uses a separate Decision + AuthorizationGrant for each operation. The
deterministic verifier remains responsible for final repair closure after those
providers return.

The legacy SQLite row remains writable only as a compatibility projection for
the existing HTTP/Feishu/state-machine API.  Its status updates are mirrored
to the canonical Work/Run when the portable store is attached.  Historical
rows continue to be imported with `dual_write_repair`.

## Workflow ownership

| Transition | Owner | Terminal / retry behaviour |
| --- | --- | --- |
| Alert/task → Work/Run | Portable authority | idempotent stable ids; projection failure blocks admission |
| policy → AuthorizationGrant | personal owner policy | version/resource scoped; expiry or mismatch denies |
| request → provider | `RealityBoundary` | qualification/policy/procedure/reliability/precommit gates; fail closed |
| approved effect → host/remote | typed Git/Docker provider | separate capability, resource grant and reliability decision; Codex cannot invoke directly |
| provider result → canonical outcome | Portable Runtime | post-fencing/commit failure becomes unknown |
| canonical result → legacy status | compatibility projection | best-effort mirror; canonical state remains inspectable |
| candidate → official knowledge | Portable `KnowledgeProjection` | typed verification/evidence/decision/grant/scope must promote first; legacy playbook is a projection |
| repair closure | independent verifier + existing approval flow | failed verification never closes the repair |

The public `portable-runtime` project remains the provider-neutral base. The
private `control-plane` project remains the personal-platform superset and its
Windows launcher and supervisor intentionally enter `python -m control_plane`.
The canonical Work/Run and authority migration is an internal architecture
change, not a deprecation of the private project or its entrypoint.

## Consequences

- Production repair traffic no longer uses `_LegacyRoutingBoundary` when the
  app creates its personal Runtime.
- Portable Work/Run and typed authorization are durable before Codex starts.
- Procedure profile metadata is monotonic: a caller cannot turn the
  `code.edit` `standard` minimum into `minimal`.
- Compatibility callers that do not provide a Runtime retain only their
  explicitly constructed in-process compatibility adapters; production
  launchers cannot enter that path.
- The provider implementation in the private vendored profile adds a no-op
  `reconcile` method required by the portable provider protocol; the public
  `D:\agent\portable-runtime` tree is not modified.
