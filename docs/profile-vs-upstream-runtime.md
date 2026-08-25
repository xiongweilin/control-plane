# Profile vs upstream runtime

`xiongweilin/control-plane` vendors the provider-neutral `portable_runtime` core from `xiongweilin/portable-runtime` and adds a personal deployment profile. Both repositories are public; the distinction is responsibility and deployment scope, not repository visibility.

## Difference

| Area | `control-plane` profile | `portable-runtime` upstream |
|------|-------------------------|-----------------------------|
| Config | `control_plane.toml`, `control_plane.toml.example` with personal Feishu/Prometheus values | `config.example.toml` with placeholders |
| Data | `data/` (SQLite, evidence JSON) gitignored | no deployment data; tests use temp dirs |
| Secrets | `CONTROL_PLANE_API_KEY`, Feishu webhook, Prometheus URLs via env | no deployment secrets; env-only placeholders |
| Integrations | `scripts/`, `deployments/windows-personal-platform` (Windows Scheduler, VBS) | provider-neutral runtime package and examples |
| Code | vendored `src/portable_runtime` + profile-local `src/control_plane` | canonical provider-neutral `src/portable_runtime` |

`portable-runtime` owns the provider-neutral runtime implementation and protocol surface. `control-plane` vendors that core and adds the Windows/Feishu/Prometheus/Docker deployment profile and local policy. Profile-specific adapters must stay outside the upstream package unless independently promoted through the upstream repository.

## How to keep in sync

- Provider-neutral core changes happen in `portable-runtime`; `control-plane` updates only through an explicit pinned vendor sync.
- The canonical pin is recorded in `portable-runtime-pin.json`; README text must not diverge from it.
- Deployment data, secrets, machine-local configuration, and evidence remain outside Git even though the repository itself is public.
- CI runs the relevant ruff/mypy/pytest and platform checks. Product semantics are not changed during a pure vendor sync.
