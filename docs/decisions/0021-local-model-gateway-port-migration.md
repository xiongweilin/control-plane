# ADR-0021: Relocate the local model gateway ports

- Status: Accepted (2026-08-22)
- Supersedes: ADR-0012 for the local gateway port assignments; ADR-0012 remains the historical record of the original connectivity decision.

## Context

The local model gateway originally used `127.0.0.1:4000` for the Codex-facing
zstd/Responses proxy and `127.0.0.1:4001` for the internal LiteLLM listener.
Those ports are occupied by system services on this workstation. The gateway
implementation, Codex configuration, and active listeners have therefore moved
to `127.0.0.1:4100` and `127.0.0.1:4101`.

The Control Plane performs a startup model-source probe against the internal
LiteLLM listener. Leaving its configuration at `4001` makes the service appear
healthy at `/live` while publishing gateway/model connectivity failures.

## Decision

Use the following loopback-only assignments as the current local contract:

```text
127.0.0.1:4100  Codex-facing zstd/Responses proxy
127.0.0.1:4101  LiteLLM listener and Control Plane model probe
```

Synchronize the active Control Plane TOML, its example, the Python default,
tests, operator documentation, and the Codex/LiteLLM runbooks. Keep ADR-0012
unchanged so its historical `4000/4001` context remains auditable.

## Consequences

- The current Codex and LiteLLM processes avoid the system-owned ports.
- Control Plane startup diagnostics and `control_plane_model_connectivity`
  now probe the live LiteLLM listener.
- Restarting either gateway or Control Plane interrupts active local sessions;
  post-change checks must verify both health endpoints and Control Plane metrics.
- Historical ADRs may still mention `4000/4001`; those mentions are intentional
  historical leftovers, not active configuration.
