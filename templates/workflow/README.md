# Workflow template

```python
from portable_runtime.core.models import Run, Work
from portable_runtime.workflows.context import WorkflowContext

class MyWorkflow:
    id = "my-workflow"
    version = "1.0.0"

    def accepts(self, work: Work) -> bool:
        return work.kind == "my-kind"

    async def run(self, context: WorkflowContext, work: Work, run: Run) -> str:
        result = await context.invoke("text.example", instruction=work.description)
        return "succeeded" if result.status == "succeeded" else "failed"
```
