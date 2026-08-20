# Current storage

The legacy `Store` uses SQLite with WAL and full synchronous writes. IDs are
stored as text, timestamps as Unix seconds, and structured values as JSON text.
Local evidence and session summaries are files under `data/`.

The portable phase adds `SQLiteStateStore`, which stores canonical Work, Run,
Artifact, Evidence, Decision, Action, Outcome, KnowledgeItem and Event records
as JSON documents keyed by `(kind, id)`. It supports atomic export/import and
does not replace legacy tables yet.
