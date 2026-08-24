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

Disposition lineage and restoration lineage are separate. Every non-`UNRESOLVED` disposition records non-empty `resolution_basis_refs_json`, explaining why the case disposition was selected. Evidence-bearing restoration judgments independently require `restoration_proof_refs_json`. For `RESTORED`, the disposition basis is the exact canonical Work/Run pair while restoration proof lineage is the canonical completion proof bundle.

For rejection, closure requires Work and Run to carry the same explicit `human_approval_decision_ref`, the same `human_approval_action=reject`, and a durable canonical `Decision` with `decision_type=human-approval`, `selected_option=reject`, and bindings to the current repair Work and repair id. The Decision is recorded in `resolution_basis_refs_json`; it is not restoration evidence. Rejection preserves the existing restoration axis.

Rollback separates authorization from execution. A rollback Decision may materialize only explicitly scoped rollback grants; a reject Decision materializes no effect grant. `ResolutionKind.ROLLED_BACK` requires both the durable human rollback Decision and a non-empty set of typed Runtime execution receipts for the required rollback operations, with every receipt reporting `succeeded`. The disposition basis records the Decision ref plus the Runtime request refs. Failed or unknown rollback execution remains recoverable and cannot form `ROLLED_BACK`. None of these facts assert restoration verified.

`NO_ACTION_REQUIRED` and `SUPERSEDED` remain representable C2 values but have no product authority path in C3.

## Ordering and crash recovery

The irreversible ordering is:

1. canonical terminal commit;
2. legacy resolution/restoration projection;
3. legacy lifecycle closure;
4. outward completion notification;
5. candidate sedimentation or other ancillary learning work.

Control-plane and portable-runtime use separate durable stores, so this is not a cross-database transaction. A crash after step 1 creates a recoverable under-projection, not a false closure. `ClosureAuthority.reconcile_restored_projection()` is idempotent and startup calls it before stale nonterminal repairs are marked interrupted. It may only reconstruct from the existing successful canonical terminal pair and its terminal audit metadata; it never re-executes a repair and never infers restoration from legacy status text.

Historical `CLOSED + UNRESOLVED` rows are not retrospectively upgraded even if a canonical terminal pair can now be found. Already-closed `RESTORED + VERIFIED` rows are consistency-checked against the canonical proof and basis bundle; contradictions are integrity errors. Startup preserves such conflicts for human inspection rather than overwriting them with `INTERRUPTED`.

An exact canonical successful pair (`Work=completed`, `Run=succeeded`) cannot be downgraded by later legacy writes to `FAILED`, `RECOVERING`, `INTERRUPTED`, or `ROLLED_BACK`. Legacy `VERIFIED` and `CLOSED` projection may proceed without rewriting the canonical terminal pair.

## Non-goals

C3 does not change verifier check-to-obligation ownership, public API response semantics, Feishu wording beyond ordering completion notification after closure, metrics, portable-runtime semantics, or formal/Lean integration. Those remain separate later steps.
