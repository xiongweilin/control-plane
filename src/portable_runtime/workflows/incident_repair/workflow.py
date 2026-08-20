"""IncidentRepairWorkflow: observe -> diagnose -> repair -> verify -> approve -> merge -> outcome."""

from __future__ import annotations

import logging

from portable_runtime.core.models import Run, Work
from portable_runtime.workflows.context import WorkflowContext

logger = logging.getLogger(__name__)


class IncidentRepairWorkflow:
    id = "incident-repair"
    version = "1.0.0"

    def accepts(self, work: Work) -> bool:
        return work.kind in {"incident", "alert", "repair"}

    async def run(self, context: WorkflowContext, work: Work, run: Run) -> str:
        # Step 1: observe (optional)
        await context.invoke("observe.logs", instruction=f"collect logs for {work.title}")
        # Step 2: diagnose via reason provider (default Codex)
        diag = await context.invoke("reason.generate", instruction=work.description or work.title)
        if diag.status not in {"succeeded", "failed"}:
            return diag.status
        # Step 3: verify (independent)
        await context.invoke("verify.http", instruction="verify repair", url=work.metadata.get("url", ""))
        # Step 4: policy gate is external; we just record
        # For this scaffold, succeed if diag succeeded
        return "succeeded" if diag.status == "succeeded" else "failed"
