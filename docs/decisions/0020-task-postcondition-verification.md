# ADR-0020: Task completion requires an independent postcondition

## Status

Accepted — 2026-08-21

## Context

`/task` accepts arbitrary natural-language work. A Codex process returning
`exit_code == 0` proves only that the provider session terminated successfully;
it does not prove that the requested objective was satisfied. Treating that
outcome as `verified=True` made execution success indistinguishable from task
completion.

## Decision

The task path records provider success as an execution outcome first. The
default `generic-task` postcondition is deliberately scoped to **delivery**:
it finds a readable result artifact whose control header is associated with the
canonical `Work`/`Run`. This proves that a result was produced and tied to the
run. It does not prove that the natural-language objective in
`Work.description` was satisfied.

Delivery evidence must therefore remain separate from objective verification:

```text
provider outcome (exit/status)
    -> delivery evidence (run-associated artifact)
    -> objective verifier (task-specific, when available)
```

The delivery check alone must not be promoted to `verified=True`, a passed
objective-verification record, or a completed `Work`. When no objective
verifier is registered, the legacy row stays `RECOVERING` and the canonical
Work/Run stay waiting for objective verification, even when delivery evidence
exists. A future task-specific verifier may close the Work only after it
records the acceptance criteria, evidence, and verification scope it evaluated.

Task projections use `Work.kind=generic-task` and
`Run.workflow_id=personal-task`; incident payloads retain the
`incident`/`incident-repair` mapping.

## Consequences

- Provider transport success, artifact delivery, and task satisfaction remain
  separate facts with non-interchangeable scopes.
- `generic-task` has a delivery-only default contract; it does not silently
  claim objective completion.
- Future task types can add stronger objective postconditions without changing
  the authority boundary.
- Restart recovery can safely distinguish an applied effect from a verified
  task result.
