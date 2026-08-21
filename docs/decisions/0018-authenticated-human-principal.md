# ADR-0018: Derive human principals from the authenticated channel

## Status

Accepted

## Date

2026-08-21

## Context

The private HTTP approval and candidate-promotion endpoints authenticate the
request with the configured control-plane API key. Their request bodies also
carry a `decided_by` label for compatibility and operator display. Treating
that label as the canonical Portable Runtime principal would allow an
authenticated caller to claim a different identity in the Decision and
AuthorizationGrant records.

## Decision

Derive the canonical principal from the authenticated channel:

- the configured control-plane API key maps to `human:<configured-owner>`;
- a future verified Feishu identity may map to its verified owner principal;
- `decided_by` remains a display/audit field in the legacy approval row and
  canonical metadata, but it does not determine `principal_ref`.

The private configuration accepts either `owner` or `human:owner` spelling and
normalizes it to the typed `human:` namespace. Portable Runtime remains
provider-neutral; the mapping belongs to the private authentication adapter.

## Consequences

- Decision and AuthorizationGrant provenance no longer claims more identity
  than the authentication evidence proves.
- Existing clients may continue sending `decided_by` without an API break.
- Legacy approval history retains the operator-supplied display label.
- Multi-user identity mapping, if added later, must be based on verified
  channel identity rather than a request-body declaration.
