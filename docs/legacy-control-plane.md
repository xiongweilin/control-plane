# Legacy control plane

The `control_plane` package is retained as the **personal-platform** profile (`D:\agent\control-plane`).

- It still serves the Windows Task Scheduler / PowerShell / VBS wrappers under `scripts/` until `deployments/windows-personal-platform` is cut over.
- It exposes the same HTTP surface (`/healthz`, `/live`, `/ready`, `/v1/...` legacy) and Feishu `/cp` commands via `compat/legacy_control_plane.py`.
- New code must not import `control_plane` from `core/`; the only allowed bridge is `compat` (data-only, `import_legacy_repair`).

Migration is additive:

```
legacy repair row (writes) -> dual_write_repair -> Work/Run/Event (reads switch before writes stop)
```

Do not delete the legacy profile before the replacement test (§64) passes.
