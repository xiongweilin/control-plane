# Workflow authoring

Workflows accept canonical `Work` and `Run` records and request capabilities
through `Runtime.run_capability`. A workflow may depend on the stable store and
artifact interfaces, but it must not import Codex, a model SDK, Feishu,
Alertmanager or an operating-system-specific tool.

Use `EchoProvider`/fake providers to test the workflow with no network, model,
shell or external service.
