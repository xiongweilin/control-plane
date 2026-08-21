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

The task path records provider success as an execution outcome first. A task
may be finalized only after a task-specific postcondition verifier finds a
readable result artifact whose control header is associated with the canonical
`Work`/`Run`. The artifact proves that a result was produced and tied to the
run; it does not assert the truth of arbitrary natural-language content.

If the artifact or a task-specific verifier is unavailable, the legacy row
stays `RECOVERING` and the canonical Work/Run stay waiting for verification.
No `verified=True`, passed verification record, or completed Work is written.

Task projections use `Work.kind=generic-task` and
`Run.workflow_id=personal-task`; incident payloads retain the
`incident`/`incident-repair` mapping.

## Consequences

- Provider transport success and task satisfaction remain separate facts.
- Future task types can add stronger postconditions without changing the
  authority boundary.
- Restart recovery can safely distinguish an applied effect from a verified
  task result.
