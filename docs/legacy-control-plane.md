# Control-plane / portable-runtime boundary

`portable-runtime` is the public, provider-neutral runtime project. It owns
the canonical Work/Run model, provider protocol, capability effects and
RealityBoundary. `control-plane` is the private personal-platform project: it
uses that runtime as its base and adds the Windows, Feishu, Prometheus,
Alertmanager and HTTP compatibility surface.

The personal project remains a real, supported project rather than a deprecated
package. Its production entrypoint is intentionally:

```text
python -m control_plane
```

The Windows Task Scheduler / PowerShell / VBS wrappers under `scripts/` launch
that private entrypoint. `control_plane.app:create_app` exposes the personal
HTTP surface (`/healthz`, `/live`, `/ready`, `/v1/...`) and Feishu `/cp`
commands via `compat/legacy_control_plane.py`.

The authority path is now:

```
Alertmanager / task
  -> Portable Work/Run
  -> personal-owner policy + AuthorizationGrant
  -> RealityBoundary
  -> CodexProvider(code.edit)
  -> legacy repair projection + independent verifier
```

`dual_write_repair` remains the idempotent importer for restart recovery and
historical rows. The canonical migration is an internal architecture change;
it does not remove or deprecate the private `control_plane` entrypoint.

New portable core code must not import `control_plane`; the public runtime must
remain independently usable. The private profile may depend on
`portable_runtime` and may retain compatibility adapters for its existing
HTTP, Feishu and Windows contracts.
