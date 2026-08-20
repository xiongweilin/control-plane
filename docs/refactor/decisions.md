# Refactor decisions

## 2026-08-20 — additive architecture inversion

The first migration slice is additive: `portable_runtime` is introduced beside
`control_plane`. This keeps the existing repair/recovery guarantees while
providing a provider-neutral seam that can be proven with fake providers before
wrapping Codex or moving legacy storage.

## 2026-08-20 — JSON record envelope for first portable store

The initial portable SQLite store uses one JSON-record table rather than
reusing legacy repair rows. This preserves canonical IDs and model evolution,
while allowing an atomic state export/import test. A future migration can map
legacy rows once parity and retention rules are explicit.
