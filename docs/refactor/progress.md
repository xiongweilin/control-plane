# Portable Runtime refactor progress

## Completed in this change

- Frozen current inventory, dependencies, workflows, storage and baseline.
- Added provider-independent canonical models for Work/Run/Artifact/Evidence/
  Decision/Action/Outcome/KnowledgeItem/Event.
- Added CapabilityRequest/CapabilityResult, ProviderDescriptor and health
  contracts.
- Added dynamic ProviderRegistry and deterministic priority routing.
- Added in-memory and SQLite state stores with ID-preserving export/import.
- Added filesystem content-addressed artifact store.
- Added stdio JSONL manifest/message models and one-shot provider adapter.
- Added provider/trigger/workflow templates, echo example and conformance
  boundary checker.
- Added minimal stable HTTP and CLI surfaces under `portable_runtime`.
- Added a data-only legacy repair adapter and portable-local deployment
  factory; neither changes the legacy profile yet.

## Deliberately deferred

- Wrapping Codex, deterministic verifiers, Alertmanager and Feishu into the new
  interfaces; legacy behavior must first gain parity tests.
- Migrating legacy SQLite rows or deleting the legacy profile.
- Full workflow engine, plugin install daemon and alternate database backend.

These are the next migration slices, not hidden compatibility breaks.
