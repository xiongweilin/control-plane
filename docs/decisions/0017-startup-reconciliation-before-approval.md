# ADR-0017: Reconcile durable effects before restoring approval waiters

## Status

Accepted

## Date

2026-08-21

## Context

Personal Git/Docker operations persist a restart-safe reconciliation
descriptor before invoking the external effect. A process restart can leave
the descriptor open while the legacy repair is `recovering`. Treating both
`recovering` and `needs_approval` as one startup queue would restore a human
approval waiter before asking reality what happened. That conflates an
epistemic question (what effect, if any, exists?) with a normative question
(may the effect be performed?).

## Decision

The FastAPI lifespan consumes `ReconciliationDescriptorStore.list_open()`
before registering approval waiters. Each descriptor is reconciled through the
Portable Runtime capability service using its durable `request_id` and
`provider_id`; the original effect is never replayed during startup.

- `applied` enters deterministic repair verification and may close the repair;
- `not-applied` records a reauthorization/policy-retry decision point;
- `in-progress`, `concurrent-change`, and `mismatch` remain `recovering` and
  require a recovery procedure or reopen/reframe;
- `unknown`, `pending`, and `needs-reconciliation` remain `recovering` for a
  later observation or escalation.

Only rows that are still `needs_approval` after this pass are handed to
`resume_pending_approval()`.

## Consequences

- A restart cannot turn missing reality evidence into a second approval prompt.
- The descriptor remains the durable recovery authority, while the legacy
  repair row and canonical Work/Run receive a traceable lifecycle projection.
- `not-applied` and unresolved classifications do not automatically retry a
  side effect; policy or an explicitly authorized recovery procedure must make
  that decision.
