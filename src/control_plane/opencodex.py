from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import httpx


class AgentCallError(RuntimeError):
    pass


@dataclass(slots=True)
class FunctionCall:
    call_id: str
    name: str
    arguments: dict[str, Any]


@dataclass(slots=True)
class AgentResult:
    final_text: str
    calls: list[FunctionCall]
    raw: dict[str, Any]


class OpenCodexClient:
    """Minimal Responses API client for deepseek-v4-flash through the local OpenCodex proxy."""

    def __init__(
        self,
        base_url: str,
        model: str,
        timeout_seconds: int = 120,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout_seconds = timeout_seconds
        self._client = client or httpx.AsyncClient(timeout=timeout_seconds)
        self._owns_client = client is None

    async def create_response(
        self,
        *,
        instructions: str,
        tools: list[dict[str, Any]],
        messages: list[dict[str, Any]],
    ) -> AgentResult:
        payload: dict[str, Any] = {
            "model": self.model,
            "instructions": instructions,
            "input": messages,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        try:
            response = await self._client.post(
                f"{self.base_url}/responses",
                json=payload,
            )
        except httpx.HTTPError as exc:
            raise AgentCallError(f"OpenCodex request failed: {exc}") from exc
        if response.status_code >= 400:
            raise AgentCallError(
                f"OpenCodex returned HTTP {response.status_code}: {response.text[:500]}"
            )
        raw = response.json()
        return parse_response(raw)

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()


def parse_response(raw: dict[str, Any]) -> AgentResult:
    output = raw.get("output") or []
    calls: list[FunctionCall] = []
    final_text = ""
    for item in output:
        item_type = item.get("type")
        if item_type == "function_call":
            arguments = item.get("arguments") or "{}"
            try:
                parsed_arguments: dict[str, Any] = json.loads(arguments)
            except json.JSONDecodeError:
                parsed_arguments = {}
            calls.append(
                FunctionCall(
                    call_id=str(item.get("call_id") or ""),
                    name=str(item.get("name") or ""),
                    arguments=parsed_arguments,
                )
            )
        elif item_type == "message":
            content = item.get("content") or []
            for part in content:
                if part.get("type") == "output_text":
                    final_text += str(part.get("text") or "")
    return AgentResult(final_text=final_text.strip(), calls=calls, raw=raw)


def function_call_output(call_id: str, output: str) -> dict[str, Any]:
    return {
        "type": "function_call_output",
        "call_id": call_id,
        "output": output,
    }
