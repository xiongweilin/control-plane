# Durable reconciliation descriptors

`control_plane.reconciliation` records the coordinates needed to re-observe a
personal Git or Docker side effect after a timeout, process crash, or machine
restart. It is an adapter-layer module in the private `control-plane` profile;
it does not add semantics to the public `portable-runtime` repository.

## What is persisted

Each `ReconciliationDescriptor` stores:

- request identity (`request_id`, `idempotency_key`), provider identity and
  version, capability, resource reference, and subject version references;
- a redacted request snapshot (parameter names are retained, raw parameter
  values are not copied);
- the pre-effect baseline;
- an operation-specific expected postcondition;
- operation-specific observation coordinates;
- the latest observation and its non-terminal or terminal classification.

`ReconciliationDescriptorStore` persists the JSON descriptor in SQLite. The
SQLite columns are only indexes for startup queries; the JSON payload is the
schema authority. `save()` is an idempotent upsert, and `list_open()` returns
pending, unknown, in-progress, concurrent-change, mismatch, and
needs-reconciliation descriptors for recovery.

## Git merge ancestry

The provider should capture `target_baseline_commit` before the merge. During
reconciliation it should re-run Git ancestry observation and pass the facts to
`classify_git_merge_ancestry()`:

| Fresh facts | Verdict |
| --- | --- |
| Candidate is an ancestor of the target tip | `applied` |
| Target is still the baseline and candidate is absent | `not-applied` |
| Target moved from the baseline and candidate is absent | `concurrent-change` |
| `MERGE_HEAD` or conflicts remain | `in-progress` |
| Ancestry or target-tip facts are unavailable | `unknown` |

A changed target is never inferred to mean that the candidate was merged.

## Git push and Docker

`GitPushOperation` records the expected commit and remote-ref observation
coordinates. `classify_git_push_remote_ref()` returns `applied` only when the
fresh remote ref equals that commit; an unobservable ref is `unknown`.

`DockerOperation` and `DockerPostcondition` express desired-state semantics:
`classify_docker_state()` can confirm current health, but it does not claim
that a particular restart event occurred. A healthy current state therefore
must not be used as restart-event attribution.

## Integration boundary

The execution provider should persist a descriptor before invoking the external
operation, persist a `ReconciliationObservation` after every fresh observation,
and leave `unknown` or `concurrent-change` open for recovery. The authority
layer must not turn those classifications into deterministic verification
failure without a separate verifier result.
