# ADR-0019: Inject deployment-specific Codex process boundaries

## Status

Accepted — 2026-08-21

## Context

The personal profile must run Codex inside a Windows-specific boundary that
creates candidate worktrees and restricts credentials and Docker access. The
portable Codex provider is the semantic upstream and must not import
`control_plane.codex_runner`; keeping that import in the vendored provider
created a dependency loop and made the vendored file diverge from the public
provider.

## Decision

`portable_runtime.providers.codex.CodexProvider` accepts an optional,
provider-neutral `ExecutionBoundary` protocol. The protocol prepares a
working directory/environment for one invocation, exposes a session directory,
redacts transcripts, and cleans up the prepared boundary. The public provider
owns only this contract and the capability-derived sandbox mapping.

`control_plane.codex_boundary.CodexExecutionBoundaryAdapter` is the private
implementation. It delegates to the existing `CodexRunner` boundary so the
Windows worktree, credential, and Docker restrictions remain private. The
vendored provider is byte-identical to the public provider; only the injected
adapter is deployment-specific.

## Consequences

- Dependency direction is one-way: `control_plane` depends on
  `portable_runtime`; the portable provider never imports `control_plane`.
- The public runtime remains usable without a deployment boundary and keeps
  its fail-closed capability-to-sandbox semantics.
- Private host isolation remains testable without moving Windows policy into
  the public repository.
