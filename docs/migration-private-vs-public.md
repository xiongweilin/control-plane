# Private vs Public

Private repo `ratiolin/control-plane` and public library `ratiolin/portable-runtime` share the same `portable_runtime` core. Difference is private info only.

## Difference

| Area | Private (`control-plane`) | Public (`portable-runtime`) |
|------|---------------------------|-----------------------------|
| Config | `control_plane.toml`, `control_plane.toml.example` with local Feishu/Prometheus values | `config.example.toml` with placeholders |
| Data | `data/` (SQLite, evidence JSON) gitignored | no data, tests use temp dirs |
| Secrets | `CONTROL_PLANE_API_KEY`, Feishu webhook, Prometheus URLs via env | no secrets, env-only placeholders |
| Integrations | `scripts/`, `deployments/windows-personal-platform` (Windows Scheduler, VBS) | `examples/echo-provider` only |
| Code | `src/portable_runtime` (same core) | `src/portable_runtime` (same core) |

The public repository owns the provider-neutral `src/portable_runtime` core.
The private repository vendors that core and adds `src/control_plane` as the
personal-platform compatibility/deployment profile (Windows, Feishu,
Prometheus and local policy). The private profile must not change the public
`D:\agent\portable-runtime` tree; core changes are synced from public and
private-only adapters stay outside the public package.

## How to keep in sync

- Core changes happen in `portable_runtime` and are cherry-picked or pulled between repos.
- Private-only files are never committed to public: `control_plane.toml`, `data/`, keys.
- CI is same (ruff/mypy/pytest) but SonarCloud runs only for public library.
