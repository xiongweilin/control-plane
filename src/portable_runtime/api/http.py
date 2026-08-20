from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from portable_runtime.core.models import utcnow
from portable_runtime.core.runtime import Runtime


class CreateWorkRequest(BaseModel):
    title: str = Field(min_length=1, max_length=512)
    description: str = Field(default="", max_length=20_000)
    kind: str = Field(default="generic-task", min_length=1, max_length=128)
    requested_capabilities: list[str] = Field(default_factory=list)


class RunCapabilityRequest(BaseModel):
    capability: str = Field(min_length=1, max_length=256)
    instruction: str | None = Field(default=None, max_length=20_000)
    run_id: str | None = None
    parameters: dict[str, Any] = Field(default_factory=dict)


def create_app(runtime: Runtime | None = None) -> FastAPI:
    runtime = runtime or Runtime()
    app = FastAPI(title="Portable Runtime", version="0.1.0")
    app.state.runtime = runtime

    @app.get("/v1/runtime")
    async def runtime_info() -> dict[str, Any]:
        return {"runtime_id": runtime.runtime_id, "provider_count": len(runtime.registry.list())}

    @app.get("/v1/health/live")
    async def live() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/v1/health/ready")
    async def ready() -> dict[str, Any]:
        return await runtime.health()

    @app.get("/v1/providers")
    async def providers() -> list[dict[str, Any]]:
        return [descriptor.model_dump(mode="json") for descriptor in runtime.registry.list()]

    @app.get("/v1/providers/{provider_id}")
    async def get_provider(provider_id: str) -> dict[str, Any]:
        try:
            return runtime.registry.get(provider_id).descriptor.model_dump(mode="json")
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/v1/providers/{provider_id}/enable")
    async def enable_provider(provider_id: str) -> dict[str, Any]:
        try:
            return runtime.registry.enable(provider_id).model_dump(mode="json")
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/v1/providers/{provider_id}/disable")
    async def disable_provider(provider_id: str) -> dict[str, Any]:
        try:
            return runtime.registry.disable(provider_id).model_dump(mode="json")
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/v1/providers/{provider_id}/reload")
    async def reload_provider(provider_id: str) -> dict[str, Any]:
        try:
            return runtime.registry.reload(provider_id).model_dump(mode="json")
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/v1/capabilities")
    async def capabilities() -> list[str]:
        return sorted({capability for provider in runtime.registry.list() for capability in provider.capabilities})

    @app.post("/v1/work")
    async def create_work(body: CreateWorkRequest) -> dict[str, Any]:
        return runtime.create_work(
            title=body.title,
            description=body.description,
            kind=body.kind,
            requested_capabilities=body.requested_capabilities,
        ).model_dump(mode="json")

    @app.get("/v1/work")
    async def list_work(status: str | None = None) -> list[dict[str, Any]]:
        return [work.model_dump(mode="json") for work in runtime.list_work(status)]

    @app.get("/v1/work/{work_id}")
    async def get_work(work_id: str) -> dict[str, Any]:
        work = runtime.get_work(work_id)
        if work is None:
            raise HTTPException(status_code=404, detail="work not found")
        return work.model_dump(mode="json")

    @app.post("/v1/work/{work_id}/run")
    async def run_work(work_id: str, body: RunCapabilityRequest) -> dict[str, Any]:
        if runtime.get_work(work_id) is None:
            raise HTTPException(status_code=404, detail="work not found")
        result = await runtime.run_capability(
            work_id,
            body.capability,
            instruction=body.instruction,
            run_id=body.run_id,
            **body.parameters,
        )
        return result.model_dump(mode="json")

    @app.post("/v1/work/{work_id}/cancel")
    async def cancel_work(work_id: str) -> dict[str, Any]:
        work = runtime.get_work(work_id)
        if work is None:
            raise HTTPException(status_code=404, detail="work not found")
        cancelled = work.model_copy(update={"status": "cancelled", "updated_at": utcnow()})
        runtime.store.save_work(cancelled)
        return cancelled.model_dump(mode="json")

    @app.get("/v1/runs/{run_id}")
    async def get_run(run_id: str) -> dict[str, Any]:
        run = runtime.store.get_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="run not found")
        return run.model_dump(mode="json")

    @app.post("/v1/state/export")
    async def export_state() -> dict[str, Any]:
        return runtime.export_state()

    @app.post("/v1/state/import")
    async def import_state(body: dict[str, list[dict[str, object]]]) -> dict[str, str]:
        runtime.import_state(body)
        return {"status": "imported"}

    @app.get("/v1/knowledge")
    async def list_knowledge(status: str | None = None) -> list[dict[str, Any]]:
        return [item.model_dump(mode="json") for item in runtime.store.list_knowledge(status)]

    @app.get("/v1/knowledge/{knowledge_id}")
    async def get_knowledge(knowledge_id: str) -> dict[str, Any]:
        item = runtime.store.get_knowledge(knowledge_id)
        if item is None:
            raise HTTPException(status_code=404, detail="knowledge item not found")
        return item.model_dump(mode="json")

    @app.get("/v1/artifacts/{artifact_id}")
    async def get_artifact(artifact_id: str) -> dict[str, Any]:
        artifact = runtime.store.get_artifact(artifact_id)
        if artifact is None:
            raise HTTPException(status_code=404, detail="artifact not found")
        return artifact.model_dump(mode="json")

    return app
