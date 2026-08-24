# ADR 0023: Orthogonal repair resolution axes

Status: Accepted

## Context

`RepairState` is workflow lifecycle. In particular, legacy states such as `CLOSED` and `VERIFIED` do not establish whether the target reality was restored. Collapsing workflow termination, case disposition, and restoration judgment into one status would let one responsibility position silently substitute for another.

## Decision

Keep three orthogonal axes:

- `RepairState`: workflow lifecycle.
- `ResolutionKind`: case disposition (`unresolved`, `restored`, `no_action_required`, `rejected`, `rolled_back`, `superseded`, `escalated`).
- `RestorationStatus`: reality judgment (`unverified`, `verified`, `failed`, `unknown`).

`UNVERIFIED` means qualifying restoration evidence has not yet been established and makes no claim about reality. `UNKNOWN` means observation or checking occurred but the result remains indeterminate.

Persist restoration proof lineage separately as `restoration_proof_refs_json`. Structurally, `restored` and `no_action_required` require `restoration_status=verified` and at least one proof reference. C2 does not establish which product authority may make those assertions; that is deferred to C3 ClosureAuthority.

Historical rows migrate conservatively to `unknown + unresolved` with no proof refs. A legacy `CLOSED` or `VERIFIED` value is not retrospective restoration evidence. Newly created repairs initialize explicitly to `unverified + unresolved`.

## Non-goals

C2 does not change `RepairState`, transition rules, terminal states, service closure paths, portable-runtime, notifier/API wording, metrics, or startup reconciliation. It does not scan historical portable Work/Run records to reconstruct restoration judgments.

## Consequences

Lifecycle writes and resolution writes use separate storage seams. `set_repair_status()` cannot write the new axes; `set_repair_resolution()` cannot change lifecycle state or portable Work/Run records. Product authority for `restored + verified` remains intentionally absent until C3.
