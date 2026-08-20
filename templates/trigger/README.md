# Trigger template

Implements `TriggerSource`:

```python
from portable_runtime.triggers.base import TriggerDescriptor, TriggerEvent, TriggerEmitter

class MyTrigger:
    @property
    def descriptor(self) -> TriggerDescriptor:
        return TriggerDescriptor(id="my-trigger", name="My Trigger")

    async def start(self, emit: TriggerEmitter) -> None:
        # emit events via await emit(TriggerEvent(...))
        pass

    async def stop(self) -> None:
        pass
```
