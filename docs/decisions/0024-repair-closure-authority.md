# ADR 0024: Repair ClosureAuthority

Status: Accepted

## Context

C2 separated workflow lifecycle (`RepairState`), case disposition (`ResolutionKind`), and reality judgment (`RestorationStatus`). That separation intentionally left one question unanswered: which product authority may project an accepted canonical terminal fact into a legacy case disposition and close the case.

Portable-runtime already owns successful objective/verification termination through `CompletionAuthority`. A passing verifier report is persisted as typed evidence, and only portable `CompletionAuthority` may atomically commit `Work=completed` with `Run=succeeded` after proof coverage is complete. Re-running verifier logic in control-plane would create a second authority and allow divergent truth criteria.

## Decision

Introduce one private `ClosureAuthority` in `src/control_plane/closure_authority.py`.

For positive restoration closure, it consumes only an already-terminal canonical Work/Run pair. It requires:

- canonical Work status `completed`;
- canonical Run status `succeeded` and bound to that Work;
- symmetric, non-empty `_completion_proof_refs` on Work and Run;
- symmetric terminal obligation audit fields;
- `completion_missing_obligations == []`;
- declared required obligations are covered.

It derives `restoration_proof_refs` from canonical terminal metadata. Callers cannot pass `restoration_status=verified` or replacement proof refs. The authority then projects `ResolutionKind.RESTORED + RestorationStatus.VERIFIED` into legacy storage and is the only product owner allowed to form `RepairState.CLOSED` for successful restoration.

For rejection, closure requires a durable canonical `Decision` with `decision_type=human-approval`, `selected_option=reject`, and bindings to the current repair Work and repair id. The Decision is recorded in `resolution_basis_refs_json`; it is not restoration evidence. Rejection preserves the existing restoration axis.

A successful rollback similarly records `ResolutionKind.ROLLED_BACK` from a durable human rollback Decision while preserving the restoration axis. Rollback does not imply restoration.

`NO_ACTION_REQUIRED` and `SUPERSEDED` remain representable C2 values but have no product authority path in C3.

## Ordering and crash recovery

The irreversible ordering is:

1. canonical terminal commit;
2. legacy resolution/restoration projection;
3. legacy lifecycle closure;
4. outward completion notification.

Control-plane and portable-runtime use separate durable stores, so this is not a cross-database transaction. A crash after step 1 creates a recoverable under-projection, not a false closure. `ClosureAuthority.reconcile_restored_projection()` is idempotent and startup calls it before stale nonterminal repairs are marked interrupted. It may only reconstruct from the existing successful canonical terminal pair and its terminal audit metadata; it never re-executes a repair and never infers restoration from legacy status text.

Legacy projection must never downgrade an already-terminal canonical Work/Run pair. Storage therefore treats a canonical terminal pair as authoritative and ignores subsequent nonterminal legacy lifecycle projection attempts at the portable seam.

## Non-goals

C3 does not change verifier check-to-obligation ownership, public API response semantics, Feishu wording beyond ordering completion notification after closure, metrics, portable-runtime semantics, or formal/Lean integration. Those remain separate later steps.
