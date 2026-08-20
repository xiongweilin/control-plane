from portable_runtime.core.capabilities import CapabilityRequest, CapabilityResult
from portable_runtime.plugin import provider


@provider(
    id="example",
    version="1.0.0",
    capabilities=["text.example"],
)
async def invoke(request: CapabilityRequest) -> CapabilityResult:
    return CapabilityResult(
        request_id=request.id,
        provider_id="example",
        status="succeeded",
        message=request.instruction,
    )
