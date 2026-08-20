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

No code fork: both repos build `src/portable_runtime` as single-form package. Private repo does not include `src/control_plane`; legacy package was removed and history remains in Git.

## How to keep in sync

- Core changes happen in `portable_runtime` and are cherry-picked or pulled between repos.
- Private-only files are never committed to public: `control_plane.toml`, `data/`, keys.
- CI is same (ruff/mypy/pytest) but SonarCloud runs only for public library.
