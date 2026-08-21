# ADR-0014: Capability-scoped Codex sandboxes

## Status

Accepted

## Date

2026-08-21

## Context

Portable Runtime classifies `reason.generate` as `read` / `pure`, while the
Codex provider previously launched every capability with
`--sandbox danger-full-access`. The runtime contract therefore described a
read-only judgment operation while the provider still had authority to edit
files, run commands, and reach external systems.

The private Windows profile also routes the legacy repair bridge through the
portable capability seam. A repair is a candidate workspace edit, but it had
been labeled `reason.generate`, which made the semantic distinction harder to
enforce and audit.

## Decision

Codex sandbox selection is derived from the requested capability and is an
independent physical guard:

| Capability class | Sandbox |
| --- | --- |
| `reason.generate`, `code.read`, `git.diff` | `read-only` |
| `code.edit`, `code.test`, `shell.exec` | `workspace-write` |
| Unknown capability | `read-only` (fail closed) |

The personal profile rejects `danger-full-access` as a configured sandbox
value. Remote, deploy, admin, or irreversible effects must be implemented by a
separate Provider behind Portable Runtime's RealityBoundary, authorization,
and reliability stages.

Legacy repair requests use `code.edit` and include the target repository as
their local resource. `CodexRunner` accepts only `read-only` or
`workspace-write`; the compatibility adapter passes the capability-derived
ceiling when the wrapped runner supports the parameter.

## Alternatives Considered

### Keep `danger-full-access` and rely on prompts

Rejected. Prompt constraints are not a physical capability boundary and do not
make the provider's effect contract truthful.

### Change `reason.generate` to a write capability

Rejected. Reasoning and candidate modification are different responsibilities;
the correct fix is to label edits as `code.edit` and constrain the process
accordingly.

### Add a global approval boolean

Rejected. A boolean does not identify the actor, resource, capability, or
version ceiling. Typed authorization remains a Runtime concern for action
critical capabilities.

## Consequences

- Read-only Codex sessions cannot modify the workspace through the Codex
  sandbox.
- Candidate edits remain local to the requested workspace and continue through
  the existing independent verifier, approval, merge, and rollback flow.
- Existing injected legacy test doubles without a `sandbox` parameter remain
  compatible; production `CodexRunner` instances receive the ceiling.
- The vendored provider has a small private-profile hardening delta relative to
  upstream. If upstream adds an equivalent capability-scoped sandbox hook, the
  local delta should be removed in the next sync.
