"""Workflow template — describes how to do a task, never imports a concrete provider."""
from portable_runtime.core.models import Run, Work
from portable_runtime.workflows.context import WorkflowContext


class MyWorkflow:
    """Copy this file to create a new workflow."""

    id = "my-workflow"
    version = "1.0.0"

    def accepts(self, work: Work) -> bool:
        return work.kind == "my-kind"

    async def run(self, context: WorkflowContext, work: Work, run: Run) -> str:
        # Only call context.invoke(capability, ...) — never import providers directly
        result = await context.invoke("text.example", instruction=work.description)
        if result.status == "succeeded":
            return "succeeded"
        if result.status == "needs-input":
            return "waiting"
        if result.status == "unavailable":
            return "blocked"
        return "failed"
