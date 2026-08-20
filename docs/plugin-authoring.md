# Plugin authoring

1. Copy `templates/provider-python/` or implement the stdio-jsonl contract.
2. Give the provider a stable ID and capability names.
3. Run `runtime plugin validate path/to/plugin`.
4. Run `runtime plugin test path/to/plugin` for the provider conformance probe.
5. Register the provider with `runtime.registry.register(...)` in the host
   profile and enable/disable it without changing core or database schemas.

The smallest useful provider implements `descriptor`, `health` and `invoke`.
`cancel` may be a no-op when the provider explicitly does not support it.
