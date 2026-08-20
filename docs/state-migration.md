# State migration

Export a portable Runtime state as JSON:

```python
state = runtime.export_state()
runtime.import_state(state)
```

SQLite import is transactional and idempotent by `(kind, id)`. Legacy repair
rows are intentionally not silently guessed into Work/Run records; the next
migration slice will add an explicit `repair_id ↔ work_id/run_id` mapping and
parity tests.
