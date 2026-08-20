# ADR-0013: Additive provider-neutral runtime seam

## Status
Accepted

## Date
2026-08-20

## Context

The legacy control plane directly wires Codex, Alertmanager, Feishu,
Prometheus, Docker, Windows deployment and SQLite. Replacing those dependencies
in one move would risk repair recovery, approvals and audit behavior.

## Decision

Introduce `portable_runtime` as an additive package. Its core owns canonical
Work/Run/artifact/evidence/knowledge state and capability routing. Providers,
triggers, stores and deployments implement stable interfaces outside core. The
legacy `control_plane` package remains the first compatibility profile until
parity tests prove each migration slice.

## Alternatives considered

- **Big-bang rename/move:** rejected because it combines behavior changes with
  a large import/storage migration and weakens rollback.
- **Prompt-centric adapter:** rejected because prompts are provider session
  details, not durable task state.
- **Global model router:** rejected because capability providers must be
  replaceable and may be absent entirely.

## Consequences

- New providers can be tested without importing legacy code.
- State export/import is explicit and provider-independent.
- Two packages coexist temporarily; later slices must add parity tests and
  maintain the legacy profile.
