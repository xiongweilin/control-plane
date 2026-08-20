# Provider API

Implement the `CapabilityProvider` contract:

```python
from portable_runtime.core.capabilities import (
    CapabilityRequest, CapabilityResult, InvocationContext,
    ProviderDescriptor, ProviderHealth,
)

class UppercaseProvider:
    @property
    def descriptor(self) -> ProviderDescriptor: ...
    async def health(self) -> ProviderHealth: ...
    async def invoke(self, request: CapabilityRequest, context: InvocationContext) -> CapabilityResult: ...
    async def cancel(self, request_id: str) -> None: ...
```

For a small in-process provider, `portable_runtime.plugin.provider` can wrap a
single async handler and keep the registry/conformance contract out of the
provider's business code.

Register it at runtime:

```python
runtime.registry.register(UppercaseProvider())
runtime.registry.disable("uppercase")
runtime.registry.enable("uppercase")
```

Providers return structured status and references. They must not write Runtime
state directly; the Runtime records Action/Outcome history around invocation.
