from __future__ import annotations

import asyncio
import json
import re
import urllib.parse
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from .config import ControlPlaneConfig
from .storage import Store

IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
CONTAINER_STATUS_FORMAT = "{{.Names}}\t{{.Status}}"


class ToolError(RuntimeError):
    def __init__(self, message: str, *, requires_approval: bool = False) -> None:
        super().__init__(message)
        self.requires_approval = requires_approval


@dataclass(slots=True)
class ToolResult:
    output: str
    status: str = "ok"
    before: dict[str, Any] | None = None
    after: dict[str, Any] | None = None
    requires_approval: bool = False
    target_kind: str = ""


class ToolContext:
    def __init__(
        self,
        config: ControlPlaneConfig,
        store: Store,
        repair_id: str,
        patch_dir: Path,
        executor: CommandExecutor | None = None,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self.config = config
        self.store = store
        self.repair_id = repair_id
        self.patch_dir = patch_dir
        self.executor = executor or CommandExecutor(config)
        self.http = http or httpx.AsyncClient(timeout=30)
        self._owns_http = http is None

    async def close(self) -> None:
        if self._owns_http:
            await self.http.aclose()


def container_status_args(project: str) -> list[str]:
    return [
        "docker",
        "ps",
        "--filter",
        f"label=com.docker.compose.project={project}",
        "--format",
        CONTAINER_STATUS_FORMAT,
    ]


class CommandExecutor:
    """Runs native Windows commands (Docker Desktop, git) directly; WSL retired 2026-08-07."""

    def __init__(self, config: ControlPlaneConfig) -> None:
        self.config = config

    async def run(
        self,
        args: list[str],
        *,
        cwd: str | None = None,
        timeout: int = 60,
        input_text: str | None = None,
    ) -> str:
        proc = await asyncio.create_subprocess_exec(
            *args,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.PIPE if input_text is not None else asyncio.subprocess.DEVNULL,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(input_text.encode("utf-8") if input_text is not None else None),
                timeout=timeout,
            )
        except TimeoutError as exc:
            proc.kill()
            raise ToolError(f"Command timed out after {timeout}s") from exc
        output = (stdout.decode("utf-8", errors="replace") + stderr.decode("utf-8", errors="replace")).strip()
        if proc.returncode != 0:
            raise ToolError(f"Command failed (exit {proc.returncode}): {output[-2000:]}")
        return output


def validate_identifier(value: str, label: str) -> str:
    if not IDENTIFIER_RE.fullmatch(value):
        raise ToolError(f"Invalid {label}: {value!r}")
    return value


def validate_url(value: str, allowed_origins: tuple[str, ...]) -> str:
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme not in {"http", "https"}:
        raise ToolError(f"Unsupported URL scheme: {parsed.scheme}")
    origin = f"{parsed.scheme}://{parsed.netloc}"
    if not any(origin.startswith(prefix) for prefix in allowed_origins):
        raise ToolError(f"URL origin not allowed: {origin}")
    return value


def resolve_repo(value: str, allowed_roots: tuple[str, ...]) -> str:
    normalized = value.replace("\\", "/").rstrip("/")
    allowed = tuple(root.replace("\\", "/").rstrip("/") for root in allowed_roots)
    if not any(normalized == root or normalized.startswith(root + "/") for root in allowed):
        raise ToolError(f"Repository path not allowed: {value}")
    return normalized


async def _probe(http: httpx.AsyncClient, url: str, timeout: int = 20) -> tuple[bool, str]:
    try:
        response = await http.get(url, timeout=timeout, follow_redirects=False)
    except httpx.HTTPError as exc:
        return False, f"probe failed: {exc}"
    if response.status_code in {200, 401}:
        return True, f"HTTP {response.status_code}"
    return False, f"HTTP {response.status_code}"


async def _run_cmd(ctx: ToolContext, args: list[str], cwd: str | None = None) -> str:
    return await ctx.executor.run(args, cwd=cwd)


async def _container_status_tool(ctx: ToolContext, arguments: dict[str, Any]) -> ToolResult:
    project = validate_identifier(str(arguments.get("project", "")), "project")
    if project not in ctx.config.allowed_auto_projects:
        raise ToolError(f"Project not allowed for inspection: {project}")
    output = await _run_cmd(
        ctx,
        container_status_args(project),
    )
    return ToolResult(output=output or "(no containers)", target_kind="inspection")


async def _read_logs_tool(ctx: ToolContext, arguments: dict[str, Any]) -> ToolResult:
    project = validate_identifier(str(arguments.get("project", "")), "project")
    if project not in ctx.config.allowed_auto_projects:
        raise ToolError(f"Project not allowed for logs: {project}")
    service = validate_identifier(str(arguments.get("service", "")), "service")
    lines = max(1, min(int(arguments.get("lines", 100)), 500))
    output = await _run_cmd(
        ctx,
        [
            "docker",
            "logs",
            "--tail",
            str(lines),
            "--format",
            "{{.Name}}\t{{.Message}}",
            "--filter",
            f"label=com.docker.compose.project={project}",
            "--filter",
            f"label=com.docker.compose.service={service}",
        ],
    )
    return ToolResult(output=output[:10_000] or "(no logs)", target_kind="inspection")


async def _probe_tool(ctx: ToolContext, arguments: dict[str, Any]) -> ToolResult:
    url = validate_url(str(arguments.get("url", "")), ctx.config.allowed_url_origins)
    ok, detail = await _probe(ctx.http, url)
    return ToolResult(
        output=f"{url}: {detail}",
        after={"probe_urls": [url]},
        target_kind="inspection",
    )


async def _promql_tool(ctx: ToolContext, arguments: dict[str, Any]) -> ToolResult:
    query = str(arguments.get("query", ""))[:1_024]
    if not query:
        raise ToolError("Missing PromQL query")
    try:
        response = await ctx.http.get(
            f"{ctx.config.prometheus_url}/api/v1/query",
            params={"query": query},
            timeout=20,
        )
        response.raise_for_status()
        body = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise ToolError(f"Prometheus query failed: {exc}") from exc
    return ToolResult(
        output=json.dumps(body.get("data", {}), ensure_ascii=False)[:10_000],
        after={"promql": {"check": query}},
        target_kind="inspection",
    )


async def _git_status_tool(ctx: ToolContext, arguments: dict[str, Any]) -> ToolResult:
    repo = resolve_repo(str(arguments.get("repo", "")), ctx.config.allowed_repo_roots)
    output = await _run_cmd(ctx, ["git", "-C", repo, "status", "--short", "--branch"])
    return ToolResult(output=output or "(clean)", target_kind="inspection")


async def _restart_service_tool(ctx: ToolContext, arguments: dict[str, Any]) -> ToolResult:
    project = validate_identifier(str(arguments.get("project", "")), "project")
    if project not in ctx.config.allowed_auto_projects:
        raise ToolError(f"Project not allowed for restart: {project}")
    service = validate_identifier(str(arguments.get("service", "")), "service")
    project_dir = ctx.config.project_dirs.get(project, f"D:\\infrastructure\\compose\\{project}")
    before = await _run_cmd(
        ctx,
        container_status_args(project),
    )
    if service:
        await _run_cmd(ctx, ["docker", "compose", "restart", service], cwd=str(project_dir), timeout=180)
    else:
        await _run_cmd(ctx, ["docker", "compose", "restart"], cwd=str(project_dir), timeout=180)
    after = await _run_cmd(
        ctx,
        container_status_args(project),
    )
    return ToolResult(
        output=after or "(no containers)",
        before={"containers": before},
        after={"containers": after},
        target_kind="service",
    )


async def _compose_up_tool(ctx: ToolContext, arguments: dict[str, Any]) -> ToolResult:
    project = validate_identifier(str(arguments.get("project", "")), "project")
    if project not in ctx.config.allowed_auto_projects:
        raise ToolError(f"Project not allowed for compose up: {project}")
    project_dir = ctx.config.project_dirs.get(project, f"D:\\infrastructure\\compose\\{project}")
    before = await _run_cmd(
        ctx,
        container_status_args(project),
    )
    await _run_cmd(ctx, ["docker", "compose", "up", "-d"], cwd=str(project_dir), timeout=300)
    after = await _run_cmd(
        ctx,
        container_status_args(project),
    )
    return ToolResult(
        output=after or "(no containers)",
        before={"containers": before},
        after={"containers": after},
        target_kind="service",
    )


async def _wait_health_tool(ctx: ToolContext, arguments: dict[str, Any]) -> ToolResult:
    url = validate_url(str(arguments.get("url", "")), ctx.config.allowed_url_origins)
    timeout = max(5, min(int(arguments.get("timeout", 60)), 300))
    deadline = asyncio.get_event_loop().time() + timeout
    while True:
        ok, detail = await _probe(ctx.http, url)
        if ok:
            return ToolResult(output=f"{url}: healthy ({detail})", target_kind="verification")
        if asyncio.get_event_loop().time() >= deadline:
            raise ToolError(f"Health check timed out: {url} ({detail})")
        await asyncio.sleep(5)


async def _cleanup_docker_tool(ctx: ToolContext, arguments: dict[str, Any]) -> ToolResult:
    mode = str(arguments.get("mode", "builder"))
    if mode not in {"builder", "images"}:
        raise ToolError(f"Unsupported cleanup mode: {mode}")
    dry_run = bool(arguments.get("dry_run", True))
    system_df = await _run_cmd(ctx, ["docker", "system", "df", "--format", "{{json .}}"])
    reclaimable_gb = 0.0
    try:
        data = json.loads(system_df)
        for entry in data if isinstance(data, list) else [data]:
            if entry.get("Type") == "Build Cache":
                reclaimable_gb = float(entry.get("Reclaimable") or 0)
    except (json.JSONDecodeError, ValueError, TypeError):
        pass
    if reclaimable_gb < ctx.config.docker_cleanup_min_reclaimable_gb:
        return ToolResult(
            output=f"Reclaimable {mode} cache is {reclaimable_gb:.2f} GB; below threshold, skipping",
            before={"reclaimable_gb": reclaimable_gb},
            target_kind="maintenance",
        )
    if dry_run:
        return ToolResult(
            output=f"Dry-run: would prune {mode} ({reclaimable_gb:.2f} GB reclaimable)",
            before={"reclaimable_gb": reclaimable_gb},
            target_kind="maintenance",
        )
    command = ["docker", "builder", "prune", "-f"] if mode == "builder" else ["docker", "image", "prune", "-f"]
    await _run_cmd(ctx, command, timeout=600)
    return ToolResult(
        output=f"Pruned {mode}",
        before={"reclaimable_gb": reclaimable_gb},
        after={"pruned": mode},
        target_kind="maintenance",
    )


async def _stage_code_candidate_tool(ctx: ToolContext, arguments: dict[str, Any]) -> ToolResult:
    repo = resolve_repo(str(arguments.get("repo", "")), ctx.config.allowed_repo_roots)
    summary = str(arguments.get("summary", ""))[:200]
    patch = str(arguments.get("patch", ""))
    if not summary:
        raise ToolError("Missing candidate summary")
    if not patch:
        raise ToolError("Missing patch")
    branch = f"fix/control-plane-{ctx.repair_id}"
    patch_path = ctx.config.patch_dir / f"cp-{ctx.repair_id}.patch"
    patch_path.parent.mkdir(parents=True, exist_ok=True)
    patch_path.write_text(patch, encoding="utf-8")
    await ctx.executor.run(["git", "checkout", "-B", branch], cwd=repo, timeout=60)
    await ctx.executor.run(["git", "apply", "--check", str(patch_path)], cwd=repo, timeout=60)
    await ctx.executor.run(["git", "apply", str(patch_path)], cwd=repo, timeout=60)
    await ctx.executor.run(["git", "add", "-A"], cwd=repo, timeout=60)
    await ctx.executor.run(["git", "commit", "-m", f"control-plane: {summary}"], cwd=repo, timeout=60)
    output = await ctx.executor.run(["git", "rev-parse", "HEAD"], cwd=repo, timeout=30)
    return ToolResult(
        output=f"Candidate branch {branch} created: {output}",
        before={"repo": repo},
        after={"branch": branch, "repo": repo},
        requires_approval=True,
        target_kind="code",
    )


TOOL_SPECS: dict[str, dict[str, Any]] = {
    "get_container_status": {
        "description": "Inspect containers of an allowed compose project.",
        "parameters": {
            "type": "object",
            "properties": {"project": {"type": "string"}},
            "required": ["project"],
        },
        "handler": _container_status_tool,
        "auto": True,
    },
    "read_logs": {
        "description": "Read recent container logs for a service in an allowed project.",
        "parameters": {
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "service": {"type": "string"},
                "lines": {"type": "integer", "minimum": 1, "maximum": 500},
            },
            "required": ["project", "service"],
        },
        "handler": _read_logs_tool,
        "auto": True,
    },
    "probe_url": {
        "description": "HTTP(S) probe an allowed URL; 200/401 count as healthy.",
        "parameters": {
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
        },
        "handler": _probe_tool,
        "auto": True,
    },
    "run_promql": {
        "description": "Run a PromQL query against the local Prometheus.",
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
        "handler": _promql_tool,
        "auto": True,
    },
    "git_status": {
        "description": "Read git status of an allowed repository.",
        "parameters": {
            "type": "object",
            "properties": {"repo": {"type": "string"}},
            "required": ["repo"],
        },
        "handler": _git_status_tool,
        "auto": True,
    },
    "restart_service": {
        "description": "Restart a service (or full project) in an allowed compose project.",
        "parameters": {
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "service": {"type": "string", "description": "Optional service name"},
            },
            "required": ["project"],
        },
        "handler": _restart_service_tool,
        "auto": True,
    },
    "compose_up": {
        "description": "Run docker compose up -d for an allowed project.",
        "parameters": {
            "type": "object",
            "properties": {"project": {"type": "string"}},
            "required": ["project"],
        },
        "handler": _compose_up_tool,
        "auto": True,
    },
    "wait_health": {
        "description": "Wait until an allowed URL returns 200/401.",
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "timeout": {"type": "integer", "minimum": 5, "maximum": 300},
            },
            "required": ["url"],
        },
        "handler": _wait_health_tool,
        "auto": True,
    },
    "cleanup_docker": {
        "description": "Prune docker builder cache or images when reclaimable size is above threshold.",
        "parameters": {
            "type": "object",
            "properties": {
                "mode": {"type": "string", "enum": ["builder", "images"]},
                "dry_run": {"type": "boolean"},
            },
            "required": ["mode"],
        },
        "handler": _cleanup_docker_tool,
        "auto": True,
    },
    "stage_code_candidate": {
        "description": "Create a candidate branch with a patch in an allowed repo. Requires approval before merge.",
        "parameters": {
            "type": "object",
            "properties": {
                "repo": {"type": "string"},
                "summary": {"type": "string"},
                "patch": {"type": "string"},
            },
            "required": ["repo", "summary", "patch"],
        },
        "handler": _stage_code_candidate_tool,
        "auto": False,
    },
}


async def execute_tool(
    ctx: ToolContext,
    name: str,
    arguments: dict[str, Any],
) -> ToolResult:
    spec = TOOL_SPECS.get(name)
    if spec is None:
        raise ToolError(f"Unknown tool: {name}")
    handler: Callable[[ToolContext, dict[str, Any]], Awaitable[ToolResult]] = spec["handler"]
    return await handler(ctx, arguments)
