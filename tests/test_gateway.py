from __future__ import annotations

from unittest.mock import AsyncMock

import httpx
import pytest

from control_plane.gateway import (
    AgentCallError,
    GatewayClient,
    parse_response,
)


def _raw_response(output=None, status="completed", refusal=None):
    raw = {"id": "resp-1", "status": status, "output": output or []}
    if refusal:
        raw["refusal"] = refusal
    return raw


def test_parse_function_call_and_message() -> None:
    raw = _raw_response(
        [
            {
                "type": "function_call",
                "call_id": "call-1",
                "name": "read_file",
                "arguments": '{"path": "D:\\\\x"}',
            },
            {
                "type": "message",
                "content": [{"type": "output_text", "text": "done"}],
            },
        ]
    )
    result = parse_response(raw)
    assert result.final_text == "done"
    assert len(result.calls) == 1
    assert result.calls[0].arguments == {"path": "D:\\x"}
    assert result.calls[0].arguments_parse_error == ""
    assert result.incomplete is False
    assert result.refused is False


def test_parse_keeps_function_call_argument_parse_error() -> None:
    raw = _raw_response(
        [
            {
                "type": "function_call",
                "call_id": "call-2",
                "name": "stage_code_candidate",
                "arguments": "{broken json",
            }
        ]
    )
    result = parse_response(raw)
    assert len(result.calls) == 1
    assert result.calls[0].arguments == {}
    assert "Expecting" in result.calls[0].arguments_parse_error


def test_parse_refusal_variants() -> None:
    top_level = parse_response(_raw_response(refusal="I cannot do that"))
    assert top_level.refused is True
    output_item = parse_response(_raw_response([{"type": "refusal", "refusal": "no"}]))
    assert output_item.refused is True
    content_part = parse_response(
        _raw_response(
            [{"type": "message", "content": [{"type": "refusal", "refusal": "no"}]}]
        )
    )
    assert content_part.refused is True


def test_parse_incomplete_and_failed_status() -> None:
    assert parse_response(_raw_response(status="incomplete")).incomplete is True
    assert parse_response(_raw_response(status="failed")).incomplete is True
    assert parse_response(_raw_response(status="completed")).incomplete is False


def test_parse_collects_unknown_output_types() -> None:
    raw = _raw_response(
        [
            {"type": "web_search_call", "id": "x"},
            {"type": "message", "content": [{"type": "audio", "id": "y"}]},
        ]
    )
    result = parse_response(raw)
    assert result.unknown_output_types == ["audio", "web_search_call"]


@pytest.mark.asyncio
async def test_error_body_is_redacted_before_raising() -> None:
    mock_client = AsyncMock()
    mock_client.post.return_value = httpx.Response(
        401,
        text='{"error": {"message": "invalid api_key supersecret123"}}',
    )
    client = GatewayClient("http://127.0.0.1:1/v1", "m", client=mock_client)
    with pytest.raises(AgentCallError) as exc_info:
        await client.create_response(instructions="i", tools=[], messages=[])
    message = str(exc_info.value)
    assert "supersecret123" not in message
    assert "***" in message
    assert "request_id=" in message
    await client.close()


@pytest.mark.asyncio
async def test_request_id_header_sent() -> None:
    mock_client = AsyncMock()
    mock_client.post.return_value = httpx.Response(
        200,
        json={"id": "r", "status": "completed", "output": []},
    )
    client = GatewayClient(
        "http://127.0.0.1:1/v1",
        "m",
        client=mock_client,
    )
    await client.create_response(instructions="i", tools=[], messages=[])
    kwargs = mock_client.post.call_args.kwargs
    assert "Authorization" not in kwargs["headers"]
    assert kwargs["headers"]["X-Request-Id"].startswith("cp-")
    await client.close()


@pytest.mark.asyncio
async def test_list_models_ok_and_unreachable() -> None:
    mock_client = AsyncMock()
    mock_client.get.return_value = httpx.Response(
        200,
        json={"object": "list", "data": [{"id": "a"}, {"id": "b"}]},
    )
    client = GatewayClient("http://127.0.0.1:1/v1", "m", client=mock_client)
    assert await client.list_models() == ["a", "b"]
    await client.close()

    failing = AsyncMock()
    failing.get.side_effect = httpx.ConnectError("refused")
    client2 = GatewayClient("http://127.0.0.1:1/v1", "m", client=failing)
    assert await client2.list_models() is None
    await client2.close()


def test_loopback_network_boundary_validation() -> None:
    from control_plane.config import ConfigurationError, _validate_gateway_network

    _validate_gateway_network("http://127.0.0.1:4101/v1")
    _validate_gateway_network("http://localhost:4101/v1")
    with pytest.raises(ConfigurationError, match="loopback"):
        _validate_gateway_network("http://10.0.0.5:4101/v1")
