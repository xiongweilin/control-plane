# Migration: portable_runtime extraction

## Goal

Single-form private repo and single-form public lib; difference is only private info.

- **Private** `ratiolin/control-plane` (after §64): `portable_runtime` + `profiles/personal-platform` (`control_plane` becomes thin compat/shim or archived), plus Windows/Feishu/Docker glue under `deployments/windows-personal-platform` and `scripts/`.
- **Public** `ratiolin/portable-runtime`: `portable_runtime` core (store/work/run/event/provider/plugin/verifier), no Feishu token, no metratio URLs, no `D:\agent` paths, placeholder `examples/echo-provider`.

Diff is config/secrets only: `control_plane.toml` / `data/` / `CONTROL_PLANE_API_KEY` / Feishu webhook / Prometheus remote URLs.

## Steps (additive, not destructive)

1. Publish `portable_runtime` as `ratiolin/portable-runtime` (MIT, CI + SonarCloud green, README rewrite).
2. Keep dual packages in private repo until replacement tests pass; writes go to legacy `repair` rows and `dual_write_repair` mirrors to `Work/Run/Event`.
3. Readers switch via `portable_runtime.compat.import_legacy_repair` and `dual_write` helpers.
4. Gate `§64 replacement test`: portable paths fully replace legacy call sites under same semantics; evidence/coverage preserved.

## Private repo single-form target

`
src/portable_runtime/      # primary runtime (public parity)
src/control_plane/         # DEPRECATED - compat only, archived after §64 (see README banner + docs/legacy-control-plane.md)
profiles/personal-platform/ # future: legacy config + windows/feishu profile (not yet split)
deployments/windows-personal-platform/
scripts/                   # windows scheduler wrappers stay in private profile
data/                      # gitignored, private only
`

Do NOT delete legacy before §64. The deprecation banner in `README.md` and `docs/legacy-control-plane.md` marks `control_plane` as deprecated; new code must use `portable_runtime`.

## Public lib single-form target

`
src/portable_runtime/
examples/echo-provider/
docs/
tests/
`

No private imports. No `D:\agent` absolute paths. Secrets via env only.

## pyproject transition

Current (private, dual):

`	oml
[tool.hatch.build.targets.wheel]
packages = ["src/control_plane", "src/portable_runtime"]
`

Future (private, after extract):

`	oml
dependencies = ["portable-runtime @ git+ssh://github.com/ratiolin/portable-runtime.git"]
# + packages = ["src/control_plane"] as thin shim or removed
`

Comment in `pyproject.toml` tracks this; do not change packages until §64 passes.

## Gates

- `§64`: replacement tests prove portable store/work/run cover legacy repair flows.
- `ruff check` / `mypy` / `pytest` / coverage baseline must stay green during transition.
- No push of private secrets to public lib; scan `control_plane.toml` and `data/` before publish.

## 2026-08-21: vendored portable_runtime synced to upstream 4de0284

- `src/portable_runtime` replicated byte-for-byte from `ratiolin/portable-runtime` HEAD `4de0284` (strict-enforcement P1/P2 closure; 92 tracked files, zero local drift).
- The legacy `control_plane` package stays the compatibility profile (additive seam per ADR-0013); no vendored portable file was locally patched.
- Portable V2 `CapabilityService` routes every invocation through `RealityBoundary` governance. The deprecated legacy repair path keeps pre-V2 routing semantics via `control_plane.service._LegacyRoutingBoundary`; the portable Runtime path keeps the full boundary.
- `control_plane.tools` gained `check_container_status` / `check_logs` compat helpers for portable verifier providers' fallback imports.
- Tool config aligned with upstream in `pyproject.toml` (ruff per-file-ignores + mypy overrides for the vendored tree).
- Gates: `ruff check .`, `mypy src`, `scripts/check_portable_core_imports.py`, `pytest` 231 passed, coverage 75% (baseline 74.0). Service restarted 2026-08-21 11:41 local, run `run-1787283657-7281aae8`.
