# Architecture

Portable Runtime keeps durable state in the Runtime and treats every model, harness, tool, verifier, trigger and human channel as a replaceable provider.

```
Work / Run / Artifact / Evidence / Knowledge
                    |
              Runtime + Store
                    |
          CapabilityService + Router
                    |
             ProviderRegistry
                    |
      in-process or stdio-jsonl providers
```

## Core principles

- Core never directly depends on a model, harness, OS, message platform, monitoring system or external tool.
- Provider, Trigger, Store and Workflow are Stable Interfaces under `src/portable_runtime/interfaces`.
- All durable state (Work/Run/Artifact/Evidence/Decision/Action/Outcome/Knowledge/OpenIssue) belongs to Runtime, not Provider.
- Prompt is not a persistence model; Work stores structured `title/description/kind/inputs/constraints/acceptance_criteria/requested_capabilities`.

## Verification scope

Verification is evidence-scoped. Provider status is an execution outcome, and
an artifact bound to a canonical Run is delivery evidence; neither fact may be
silently promoted to proof that the user's objective was achieved.

For `Work.kind=generic-task`, the built-in postcondition is delivery-only. It
may establish that a readable result artifact exists for the Run, but it does
not establish objective completion for the natural-language `description`.
When no task-specific objective verifier is available, the Work/Run remain
waiting for verification. A task-specific workflow may add a stronger verifier
only when it declares the acceptance criteria and evidence scope it evaluates.

## Packages

```
src/portable_runtime/
  core/         # models, runtime, registry, router, process, policies, paths
  interfaces/   # provider, trigger, store, artifact_store, workflow, transport
  protocol/     # messages, manifest
  providers/    # codex, verifiers (http/promql/container/logs/git/tests), fake, stdio
  triggers/     # alertmanager, webhook, schedule
  interactions/ # feishu (human/notify/trigger)
  stores/       # sqlite, filesystem, memory, migration
  workflows/    # incident_repair, generic_task, daily_scan, knowledge_consolidation
  plugin/       # loader, manager, sdk, conformance
  api/          # http, cli
  deployment/   # local, personal-platform
  compat/       # legacy_control_plane
```

## Data flow

```
TriggerEvent -> WorkFactory -> Work -> Workflow -> CapabilityRequest -> Router -> Provider -> CapabilityResult -> Evidence/Artifact -> Store
```

See `docs/provider-api.md`, `docs/store-api.md`, `docs/state-migration.md`.
