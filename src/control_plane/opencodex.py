from __future__ import annotations

import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx

from .audit import redact_text

logger = logging.getLogger(__name__)

_INLINE_SECRET_RE = re.compile(
    r"(?i)\b(api[_-]?key|token|secret|password|authorization|bearer)"
    r"[\s:=]{1,3}([A-Za-z0-9._\-+/]{6,})"
)


def _redact_inline_secrets(text: str) -> str:
    """Scrub secret-looking values that appear inline in error text.

    Field-level redaction only covers values under sensitive dictionary keys;
    upstream error prose often embeds the secret inside a message value
    (e.g. ``invalid api_key supersecret123``). This pass scrubs those values too.
    """
    return _INLINE_SECRET_RE.sub(lambda match: f"{match.group(1)}=***", text)


class AgentCallError(RuntimeError):
    pass


@dataclass(slots=True)
class FunctionCall:
    call_id: str
    name: str
    arguments: dict[str, Any]
    arguments_parse_error: str = ""


@dataclass(slots=True)
class AgentResult:
    final_text: str
    calls: list[FunctionCall]
    raw: dict[str, Any]
    incomplete: bool = False
    refused: bool = False
    unknown_output_types: list[str] = field(default_factory=list)


class OpenCodexClient:
    """Minimal Responses API client for deepseek-v4-flash through the local OpenCodex proxy (OpenCode Go upstream)."""

    def __init__(
        self,
        base_url: str,
        model: str,
        timeout_seconds: int = 120,
        client: httpx.AsyncClient | None = None,
        api_key: str = "",
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.api_key = api_key
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
        request_id = f"cp-{uuid.uuid4().hex[:12]}"
        headers = {"X-Request-Id": request_id}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        try:
            response = await self._client.post(
                f"{self.base_url}/responses",
                json=payload,
                headers=headers,
            )
        except httpx.HTTPError as exc:
            raise AgentCallError(
                f"OpenCodex request failed (request_id={request_id}): {exc}"
            ) from exc
        if response.status_code >= 400:
            # Never let upstream response text into the exception verbatim:
            # field-level redaction first (batch5 item 3).
            redacted_body = _redact_inline_secrets(redact_text(response.text[:8_000]))
            raise AgentCallError(
                f"OpenCodex returned HTTP {response.status_code} "
                f"(request_id={request_id}): {redacted_body[:500]}"
            )
        raw = response.json()
        return parse_response(raw)

    async def list_models(self) -> list[str] | None:
        """List model ids exposed by the OpenCodex proxy.

        Returns ``None`` when the proxy is unreachable or returns an error, so
        callers can distinguish "proxy down" from "model missing".
        """
        try:
            response = await self._client.get(f"{self.base_url}/models")
        except httpx.HTTPError:
            return None
        if response.status_code >= 400:
            return None
        try:
            data = response.json()
        except ValueError:
            return None
        models = data.get("data") or []
        ids = [str(item.get("id", "")) for item in models if isinstance(item, dict)]
        return [model_id for model_id in ids if model_id]

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()


def parse_response(raw: dict[str, Any]) -> AgentResult:
    output = raw.get("output") or []
    calls: list[FunctionCall] = []
    final_text = ""
    refused = bool(raw.get("refusal"))
    incomplete = str(raw.get("status") or "") not in {"", "completed"}
    unknown_types: list[str] = []
    for item in output:
        item_type = item.get("type")
        if item_type == "function_call":
            arguments = item.get("arguments") or "{}"
            parse_error = ""
            try:
                parsed_arguments: dict[str, Any] = json.loads(arguments)
            except json.JSONDecodeError as exc:
                # Keep the parse failure reason instead of silently dropping it:
                # the caller decides whether the error is fatal or retryable.
                parsed_arguments = {}
                parse_error = str(exc)
            calls.append(
                FunctionCall(
                    call_id=str(item.get("call_id") or ""),
                    name=str(item.get("name") or ""),
                    arguments=parsed_arguments,
                    arguments_parse_error=parse_error,
                )
            )
        elif item_type == "message":
            content = item.get("content") or []
            for part in content:
                part_type = part.get("type")
                if part_type == "output_text":
                    final_text += str(part.get("text") or "")
                elif part_type == "refusal":
                    refused = True
                elif part_type is not None:
                    unknown_types.append(str(part_type))
        elif item_type == "refusal":
            refused = True
        elif item_type is not None:
            unknown_types.append(str(item_type))
    if unknown_types:
        logger.debug(
            "OpenCodex response contained unknown output types: %s",
            sorted(set(unknown_types)),
        )
    return AgentResult(
        final_text=final_text.strip(),
        calls=calls,
        raw=raw,
        incomplete=incomplete,
        refused=refused,
        unknown_output_types=sorted(set(unknown_types)),
    )


def function_call_output(call_id: str, output: str) -> dict[str, Any]:
    return {
        "type": "function_call_output",
        "call_id": call_id,
        "output": output,
    }
