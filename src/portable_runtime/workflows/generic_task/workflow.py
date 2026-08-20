"""Generic task workflow: routes any Work with requested_capabilities through router."""

from __future__ import annotations

from portable_runtime.core.models import Run, Work
from portable_runtime.workflows.context import WorkflowContext


class GenericTaskWorkflow:
    id = "generic-task"
    version = "1.0.0"

    def accepts(self, work: Work) -> bool:
        return True

    async def run(self, context: WorkflowContext, work: Work, run: Run) -> str:
        for cap in work.requested_capabilities or ["reason.generate"]:
            result = await context.invoke(cap, instruction=work.description or work.title)
            if result.status == "failed":
                return "failed"
        return "succeeded"
