# Portable Local Deployment

Runs anywhere without Codex / Feishu / Docker / Prometheus / Alertmanager.

This is the reference deployment for `portable-runtime` (§56). Core never imports Docker or Windows Task Scheduler.

## One-click run (uv)

```powershell
uv sync
uv run python -m portable_runtime --state data/portable-runtime.db status
uv run python -m portable_runtime --state data/portable-runtime.db work submit --title "Echo test" --description "hello" --kind generic-task
uv run python -m portable_runtime --state data/portable-runtime.db work list
uv run python -m portable_runtime --state data/portable-runtime.db plugin validate examples/echo-provider
uv run python -m portable_runtime --state data/portable-runtime.db plugin test examples/echo-provider
```

Runtime aliases via console script:

```powershell
uv run runtime --state data/portable-runtime.db status
uv run runtime --state data/portable-runtime.db work submit --title "Echo test" --kind generic-task --capability text.echo
```

Zero-provider guarantees (§4.2): with no provider registered the runtime still boots, can create/list Work, and can export/import state.

## Runtime factory

```python
from pathlib import Path
from portable_runtime.deployment.local import create_local_runtime

runtime = create_local_runtime(Path("data/portable-runtime.db"), Path("data/artifacts"))
# no Codex/Feishu/Docker required
work = runtime.create_work(title="hello", kind="generic-task")
state = runtime.export_state()
runtime.import_state(state)
```

See also:
- `src/portable_runtime/deployment/local.py` — factory (`create_local_runtime` / `create_personal_platform_runtime`)
- `src/portable_runtime/triggers/schedule/trigger.py` — `ScheduleTrigger` (asyncio scheduler replacing Windows Task Scheduler, §14)
- `src/portable_runtime/__main__.py` — `python -m portable_runtime` entrypoint
- `Dockerfile` at repo root — Core does not depend on Docker, the image only wraps the same entrypoint

## Docker (Core not dependent on Docker, §56)

```dockerfile
# see Dockerfile at repo root
FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml uv.lock* ./
RUN pip install --no-cache-dir fastapi uvicorn pydantic httpx prometheus-client
COPY src ./src
EXPOSE 18083
CMD ["python", "-m", "portable_runtime", "--state", "/data/portable-runtime.db", "status"]
```

Build & run:

```powershell
docker build -t portable-runtime:local .
docker run --rm -v ${PWD}/data:/data portable-runtime:local python -m portable_runtime --state /data/portable-runtime.db status
```

## Windows personal platform

The legacy Windows deployment (Task Scheduler / PowerShell / VBS / watchdog / Prometheus / Docker / Feishu) lives in `deployments/windows-personal-platform/` and is equivalent to `profiles/personal-platform` (§57). See that directory and `docs/deployment-windows-personal-platform.md` for the full profile.
