# Personal Platform - Legacy Notes

> Personal use only. This profile wraps `portable_runtime` with local Windows/Feishu/Prometheus configuration. Diff vs public library is `control_plane.toml`/`data`/keys only.

The `control_plane` private profile at `D:\agent\control-plane` reuses the same `portable_runtime` core as the public library.

- Windows Task Scheduler / PowerShell / VBS wrappers live under `scripts/` and `deployments/windows-personal-platform`.
- HTTP surface (`/healthz`, `/live`, `/ready`) and Feishu `/cp` commands are served via `portable_runtime` interfaces (`providers`/`interactions`).
- Personal secrets (`control_plane.toml`, `data/`, `CONTROL_PLANE_API_KEY`, Feishu webhook, Prometheus URLs) stay in this private repo and are never pushed to the public library.

Public generic library: [ratiolin/portable-runtime](https://github.com/ratiolin/portable-runtime)
