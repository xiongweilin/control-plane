# ADR-0022: Disable the legacy reality bypass and use a standing owner delegation

## Status

Accepted

## Date

2026-08-22

## Context

The private compatibility service still exposed a `_LegacyRoutingBoundary`
that selected a provider and invoked it without the portable runtime's
fencing, procedure, authorization, effect-contract, and durable projection
gates.  The application entrypoint already uses the portable `Runtime`, but
the compatibility constructor remained an executable alternative.

The candidate-edit qualification path also created a new
`personal-owner-policy` `Decision` for every repair.  That record looked like
a human decision even though it was minted by the executing service.  The
actual intent is a standing owner delegation for low-risk, version-scoped
candidate edits; each repair should derive a fresh grant from that durable
authority.

## Decision

- `_LegacyRoutingBoundary` remains only as a shape-compatible adapter and
  returns a typed `LegacyRoutingDisabled` result.  It never selects or invokes
  a provider.  Provider invocation is owned by the portable runtime's
  `RealityBoundary`.
- Candidate qualification reuses one durable
  `decision_personal_owner_code_edit_v1` standing-owner delegation.  A new
  `AuthorizationGrant` is still minted per repair/version, but its source,
  metadata, and `authorized-under` relation point to that standing authority.
- The standing delegation is validated on reuse; conflicting provenance is a
  hard error rather than an overwrite.

## Consequences

- Compatibility callers that have not bootstrapped a portable runtime fail
  closed before any provider side effect.  They must migrate to
  `Runtime.capabilities`.
- Human approval remains a separate event and is not synthesized by the
  execution path.
- Existing typed Work/Run, grant, qualification, and recovery semantics are
  preserved; no new ontology is required.
- Legacy unit tests that intentionally construct `RepairService` without a
  portable runtime now assert the bounded failure instead of provider success.
