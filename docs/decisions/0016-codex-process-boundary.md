# ADR-0016: Codex candidate worktree and host-control boundary

## Status

Accepted

## Date

2026-08-21

## Context

The Codex prompt says that a repair agent may propose Git/Docker effects but may
not execute them. A prompt is not a process boundary: a child process could
otherwise inherit the user's Git credential helper, SSH agent, or Docker
Desktop named pipe. A clean source checkout also allowed a write-capable Codex
session to start in the user's active worktree.

## Decision

The private profile applies these controls before every `workspace-write`
session. The app injects the boundary configuration into the actual vendored
`portable_runtime.providers.codex.CodexProvider`; the compatibility
`control_plane.CodexRunner` uses the same implementation:

1. Create a detached `git worktree` from the source repository's `HEAD` under
   the configured worktree root. Codex receives that path as its cwd. The
   worktree is forcibly removed in `finally`; any candidate branch created by
   the agent remains in the source repository for approval.
2. Build a child environment that does not inherit Git credential helpers,
   SSH-agent variables, or GitHub/Azure token variables. A temporary Git config
   disables credential helpers and points SSH/askpass to deny commands.
3. Set Docker's host, context and config to a profile-specific disabled target
   (`control-plane-codex-disabled`) so the child does not inherit Docker
   Desktop's named pipe or local auth configuration.
4. Keep the typed Git/Docker providers outside the Codex process. They receive
   separate Runtime-authorized requests and are the only path for merge, push,
   restart and compose-up effects.

Read-only sessions retain the requested repository cwd but still receive the
credential and Docker environment guards. Non-Git compatibility targets are
left unchanged so old test doubles and diagnostic callers continue to work;
production `code.edit` repair targets are Git repositories.

## Consequences

- A Codex edit cannot dirty the user's active checkout, and credential-bearing
  Git/SSH/Docker defaults are not inherited by the session.
- Candidate branch refs are preserved for deterministic diff, verification and
  approval after the temporary worktree is removed.
- This is a Windows/Codex CLI process-level boundary, not a replacement for an
  OS restricted token. A future stronger isolation profile may additionally
  deny direct access to explicitly named Docker pipes or absolute credential
  paths with Windows ACLs; such a change requires deployment-specific testing.
- Configuration knobs are explicit in `[agent]`:
  `isolate_worktree`, `disable_docker`, `disable_ssh_credentials` and
  `worktree_root`.
