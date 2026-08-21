> [!WARNING]
> Legacy `control_plane` is no longer a production entrypoint. Portable
> Runtime (`portable_runtime`) owns the personal-platform entrypoint; this
> package remains only as the private profile implementation until its
> internals are physically absorbed.

> **Migration path**: Alertmanager/task input first materialises canonical
> `Work/Run` in Portable Runtime; the legacy repair row is then maintained as
> a compatibility projection.  The legacy package remains only for the HTTP,
> Feishu and Windows deployment surface behind the Portable Runtime profile
> entrypoint.

# Legacy control plane

The `control_plane` package is retained as the **personal-platform** profile (`D:\agent\control-plane`).

- The Windows Task Scheduler / PowerShell / VBS wrappers under `scripts/` now
  launch `portable_runtime.deployment.personal_platform`.
- It exposes the same HTTP surface (`/healthz`, `/live`, `/ready`, `/v1/...` legacy) and Feishu `/cp` commands via `compat/legacy_control_plane.py`.
- New code must not import `control_plane` from `core/`; the only allowed bridge is `compat` (data-only, `import_legacy_repair`).

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
historical rows.  The old `python -m control_plane` entrypoint is intentionally
removed.  Keep the profile implementation until the Windows `/live` `/ready`
replacement test (§64B) and one full recovery cycle are recorded; removing the
implementation itself is a separate source extraction.

