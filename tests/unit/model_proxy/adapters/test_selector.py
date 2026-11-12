from __future__ import annotations

import json

import pytest

from harbor_research_runner.model_proxy.adapters import (
    UnsupportedAdapterPair,
    select_adapter,
)


@pytest.mark.parametrize(
    ("upstream_api", "request_path"),
    [
        ("chat", "/v1/chat/completions"),
        ("responses", "/v1/responses"),
    ],
)
def test_matching_apis_pass_request_and_response_bytes_unchanged(
    upstream_api: str, request_path: str
) -> None:
    adapter = select_adapter(upstream_api, request_path)
    payload = {"model": "m", "messages": [{"role": "user", "content": "hello"}]}
    request, warnings = adapter.build_request(payload, "upstream-m")
    response_bytes = b'{"id":"r1","usage":{"input_tokens":2,"output_tokens":1}}'
    decoded = adapter.decode_response(response_bytes, "upstream-m", "application/json")

    assert request is payload
    assert warnings == []
    assert decoded.client_body is response_bytes
    assert decoded.upstream_body is None
    assert decoded.content_type == "application/json"
    assert decoded.usage is not None
    assert decoded.usage["prompt_tokens"] == 2


def test_chat_request_to_responses_upstream_uses_old_translation() -> None:
    adapter = select_adapter("responses", "/v1/chat/completions")
    payload = {
        "messages": [
            {"role": "user", "content": "run it"},
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "function": {"name": "run", "arguments": '{"cmd":"ls"}'},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "done"},
        ],
        "store": False,
    }
    request, warnings = adapter.build_request(payload, "m")
    response_body = {
        "id": "resp_1",
        "status": "completed",
        "output": [{"type": "message", "content": [{"type": "output_text", "text": "ok"}]}],
        "usage": {
            "input_tokens": 5,
            "output_tokens": 2,
            "input_tokens_details": {"cached_tokens": 1},
        },
    }
    decoded = adapter.decode_response(json.dumps(response_body).encode(), "m", "application/json")

    assert request["input"] == [
        {"role": "user", "content": "run it"},
        {
            "type": "function_call",
            "call_id": "call_1",
            "name": "run",
            "arguments": '{"cmd":"ls"}',
        },
        {"type": "function_call_output", "call_id": "call_1", "output": "done"},
    ]
    assert request["store"] is False
    assert warnings == []
    assert json.loads(decoded.client_body)["choices"][0]["message"]["content"] == "ok"
    assert decoded.usage is not None
    assert decoded.usage["prompt_tokens"] == 5
    assert decoded.usage["cached_read_tokens"] == 1


def test_responses_request_to_chat_upstream_is_refused() -> None:
    with pytest.raises(UnsupportedAdapterPair) as error:
        select_adapter("chat", "/v1/responses")

    assert "chat" in str(error.value)
    assert "responses" in str(error.value)


def test_unknown_upstream_api_is_refused() -> None:
    with pytest.raises(ValueError):
        select_adapter("other", "/v1/chat/completions")


def test_unknown_request_path_is_refused() -> None:
    with pytest.raises(ValueError):
        select_adapter("chat", "/v1/other")
