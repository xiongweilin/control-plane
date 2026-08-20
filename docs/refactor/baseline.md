# Baseline (2026-08-20)

- Tests: **189 passed**, 1 deprecation warning.
- Ruff: **passed** (`.venv\Scripts\python.exe -m ruff check .`).
- Mypy: **passed** (`.venv\Scripts\python.exe -m mypy src/control_plane`).
- Coverage gate: existing `scripts/check_coverage.py` remains unchanged.

`uv run` could not be used on this Windows checkout because its trampoline
failed to canonicalize the script path. Verification therefore uses the
checked-in `.venv` interpreter directly.

No legacy behavior was changed while recording this baseline.
