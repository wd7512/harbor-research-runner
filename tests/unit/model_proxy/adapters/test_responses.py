"""Tests for the Responses API adapter."""

from __future__ import annotations

import json
import logging

import pytest

from harbor_research_runner.model_proxy.adapters import responses as R
from harbor_research_runner.model_proxy.adapters.base import UpstreamDecodeError
from harbor_research_runner.model_proxy.adapters.responses import (
    ResponsesAdapter,
    ResponsesStreamTranslator,
)


@pytest.fixture
def translator() -> ResponsesStreamTranslator:
    return ResponsesStreamTranslator(model="m")


@pytest.mark.parametrize(
    "dropped_key",
    [
        "logit_bias",
        "logprobs",
        "user",
        "response_format",
        "stop",
        "tool_choice",
    ],
    ids=[
        "logit_bias",
        "logprobs",
        "user",
        "response_format",
        "stop",
        "tool_choice",
    ],
)
def test_translation_warnings_drop_keys(dropped_key: str) -> None:
    payloads = {
        "logit_bias": lambda: {
            "messages": [{"role": "user", "content": "hi"}],
            "logit_bias": {"a": 1},
        },
        "logprobs": lambda: {
            "messages": [{"role": "user", "content": "hi"}],
            "logprobs": True,
        },
        "user": lambda: {
            "messages": [{"role": "user", "content": "hi"}],
            "user": "u123",
        },
        "response_format": lambda: {
            "messages": [{"role": "user", "content": "hi"}],
            "response_format": {"type": "json_object"},
        },
        "stop": lambda: {
            "messages": [{"role": "user", "content": "hi"}],
            "stop": ["###"],
        },
        "tool_choice": lambda: {
            "messages": [{"role": "user", "content": "hi"}],
            "tool_choice": {"type": "function", "function": {"name": "bash"}},
        },
    }
    warnings = R.translation_warnings(payloads[dropped_key]())
    assert any(dropped_key in warning for warning in warnings)


def test_reasoning_effort_maps_to_responses_reasoning() -> None:
    """The chat `reasoning_effort` becomes responses `reasoning.effort`."""
    out = R.chat_to_responses(
        {
            "messages": [{"role": "user", "content": "hi"}],
            "reasoning_effort": "high",
        },
        "muse-spark-1.3-contributor",
    )
    assert out["reasoning"] == {"effort": "high"}
    assert "reasoning_effort" not in out


def test_reasoning_dict_passes_through_unchanged() -> None:
    """A responses-shaped `reasoning` dict is forwarded as-is."""
    reasoning = {"effort": "low", "summary": "terse"}
    out = R.chat_to_responses(
        {"messages": [{"role": "user", "content": "hi"}], "reasoning": reasoning},
        "muse-spark-1.3-contributor",
    )
    assert out["reasoning"] == reasoning


def test_reasoning_effort_wins_over_reasoning_dict() -> None:
    """When both shapes arrive, the chat shape (`reasoning_effort`) wins."""
    out = R.chat_to_responses(
        {
            "messages": [{"role": "user", "content": "hi"}],
            "reasoning": {"effort": "low"},
            "reasoning_effort": "xhigh",
        },
        "muse-spark-1.3-contributor",
    )
    assert out["reasoning"] == {"effort": "xhigh"}


def test_no_warning_for_reasoning_keys() -> None:
    """Neither reasoning shape is a dropped key anymore."""
    assert (
        R.translation_warnings(
            {
                "messages": [{"role": "user", "content": "hi"}],
                "reasoning_effort": "high",
            }
        )
        == []
    )
    assert (
        R.translation_warnings(
            {
                "messages": [{"role": "user", "content": "hi"}],
                "reasoning": {"effort": "low"},
            }
        )
        == []
    )


def test_chat_to_responses_basic() -> None:
    out = R.chat_to_responses(
        {
            "model": "ahe/muse-spark-1.3-contributor",
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 10,
            "temperature": 0.5,
            "stream": True,
            "response_format": {"type": "json_object"},
            "stream_options": {"include_usage": True},
        },
        "muse-spark-1.3-contributor",
    )
    assert out["model"] == "muse-spark-1.3-contributor"
    assert out["input"] == [{"role": "user", "content": "hi"}]
    assert out["max_output_tokens"] == 10
    assert "max_tokens" not in out
    assert out["temperature"] == 0.5
    assert out["stream"] is True
    assert "response_format" not in out
    assert "stream_options" not in out
    assert "messages" not in out


def test_chat_to_responses_tools_and_choice() -> None:
    out = R.chat_to_responses(
        {
            "messages": [],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "bash",
                        "description": "run",
                        "parameters": {"type": "object"},
                    },
                }
            ],
            "tool_choice": "auto",
        },
        "m",
    )
    assert out["tools"] == [
        {
            "type": "function",
            "name": "bash",
            "description": "run",
            "parameters": {"type": "object"},
        }
    ]
    assert out["tool_choice"] == "auto"


@pytest.mark.parametrize(
    "choice_name,kept",
    [
        ("auto", True),
        ("none", True),
        ("required", True),
        ("forced", False),
        ("forced-dict", False),
    ],
    ids=["auto", "none", "required", "forced", "forced-dict"],
)
def test_tool_choice_policy(choice_name: str, kept: bool) -> None:
    choices = {
        "auto": lambda: "auto",
        "none": lambda: "none",
        "required": lambda: "required",
        "forced": lambda: "forced",
        "forced-dict": lambda: {"type": "function", "function": {"name": "bash"}},
    }
    choice = choices[choice_name]()
    out = R.chat_to_responses({"messages": [], "tool_choice": choice}, "m")
    expected = {
        "auto": "auto",
        "none": "none",
        "required": "required",
        "forced": None,
        "forced-dict": None,
    }[choice_name]
    assert out.get("tool_choice") == expected
    assert ("tool_choice" in out) == kept


def test_chat_to_responses_tool_turn() -> None:
    out = R.chat_to_responses(
        {
            "messages": [
                {"role": "user", "content": "run ls"},
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "bash", "arguments": '{"cmd":"ls"}'},
                        }
                    ],
                },
                {"role": "tool", "tool_call_id": "call_1", "content": "ok"},
            ]
        },
        "m",
    )
    assert out["input"] == [
        {"role": "user", "content": "run ls"},
        {
            "type": "function_call",
            "call_id": "call_1",
            "name": "bash",
            "arguments": '{"cmd":"ls"}',
        },
        {"type": "function_call_output", "call_id": "call_1", "output": "ok"},
    ]


def _responses_body() -> dict:
    return {
        "id": "resp_1",
        "object": "response",
        "created_at": 1700000003,
        "status": "completed",
        "model": "muse-spark-1.3-contributor",
        "output": [
            {"type": "reasoning", "status": "completed", "encrypted_content": "x"},
            {
                "type": "message",
                "status": "completed",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "OK"}],
            },
            {
                "type": "function_call",
                "call_id": "call_9",
                "name": "bash",
                "arguments": '{"cmd":"ls"}',
            },
        ],
        "usage": {
            "input_tokens": 14,
            "output_tokens": 60,
            "total_tokens": 74,
            "input_tokens_details": {"cached_tokens": 0},
        },
    }


def test_responses_to_chat() -> None:
    chat = R.responses_to_chat(_responses_body(), "muse-spark-1.3-contributor")
    assert chat["object"] == "chat.completion"
    assert chat["created"] == 1700000003
    choice = chat["choices"][0]
    assert choice["message"]["content"] == "OK"
    assert choice["message"]["tool_calls"] == [
        {
            "id": "call_9",
            "type": "function",
            "function": {"name": "bash", "arguments": '{"cmd":"ls"}'},
        }
    ]
    assert choice["finish_reason"] == "tool_calls"
    assert chat["usage"]["prompt_tokens"] == 14
    assert chat["usage"]["completion_tokens"] == 60


def test_responses_adapter_build_and_decode() -> None:
    adapter = ResponsesAdapter()
    outbound, warnings = adapter.build_request({"messages": [], "stop": ["###"]}, "m")
    assert "messages" not in outbound
    assert warnings != []
    decoded = adapter.decode_response(
        json.dumps(_responses_body()).encode(), "m", "application/json"
    )
    assert decoded.upstream_body is not None
    assert decoded.content_type == "application/json"
    assert decoded.usage is not None
    assert decoded.usage["prompt_tokens"] == 14


def _data(obj: dict) -> dict:
    return json.loads(obj.removeprefix("data: "))


def test_sse_translator_text(translator: ResponsesStreamTranslator) -> None:
    assert (
        translator.feed({"type": "response.created", "response": {"id": "r1", "created_at": 5}})
        == []
    )
    first = translator.feed({"type": "response.output_text.delta", "delta": "O"})
    second = translator.feed({"type": "response.output_text.delta", "delta": "K"})
    assert _data(first[0])["choices"][0]["delta"] == {"content": "O"}
    assert _data(second[0])["choices"][0]["delta"] == {"content": "K"}
    assert translator.feed({"type": "response.output_item.done", "item": {}}) == []
    tail = translator.feed(
        {
            "type": "response.completed",
            "response": {
                **_responses_body(),
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "OK"}],
                    }
                ],
            },
        }
    )
    assert tail[-1] == "data: [DONE]\n\n"
    finish = _data(tail[-2])
    assert finish["choices"][0]["finish_reason"] == "stop"
    assert finish["usage"]["prompt_tokens"] == 14
    assert finish["usage"]["completion_tokens"] == 60
    assert translator.usage is not None
    assert translator.usage["prompt_tokens"] == 14


def test_tool_delta_index_is_dense_ordinal(translator: ResponsesStreamTranslator) -> None:
    translator.feed(
        {
            "type": "response.output_item.added",
            "output_index": 2,
            "item": {"type": "function_call", "call_id": "c2", "name": "write"},
        }
    )
    [chunk] = translator.feed(
        {"type": "response.function_call_arguments.delta", "output_index": 2, "delta": "{}"}
    )
    assert _data(chunk)["choices"][0]["delta"]["tool_calls"][0]["index"] == 0


def test_feed_line_skips_non_data(translator: ResponsesStreamTranslator) -> None:
    assert translator.finished is False
    assert translator.feed_line(": ping") == []
    assert translator.feed_line("") == []
    assert translator.feed_line("data: ") == []
    assert translator.feed_line("data: [DONE]") == []
    assert translator.feed_line("data: not-json") == []
    assert translator.feed_line("data: [1, 2]") == []
    assert translator.finished is False


@pytest.mark.parametrize(
    "status",
    ["failed", "cancelled"],
    ids=["failed", "cancelled"],
)
def test_failed_status_raises_upstream_error(status: str) -> None:
    body = _responses_body()
    body["status"] = status
    with pytest.raises(UpstreamDecodeError):
        R.responses_to_chat(body, "m")


@pytest.mark.parametrize(
    "status",
    ["failed", "cancelled"],
    ids=["failed", "cancelled"],
)
def test_failed_completed_event_raises(translator: ResponsesStreamTranslator, status: str) -> None:
    body = _responses_body()
    body["status"] = status
    with pytest.raises(UpstreamDecodeError):
        translator.feed({"type": "response.completed", "response": body})


def test_dict_arguments_serialized_as_json() -> None:
    out = R.chat_to_responses(
        {
            "messages": [
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "bash", "arguments": {"cmd": "ls"}},
                        }
                    ],
                }
            ]
        },
        "m",
    )
    args = out["input"][0]["arguments"]
    assert json.loads(args) == {"cmd": "ls"}


def test_partial_tool_stream_reemits_only_unseen(
    translator: ResponsesStreamTranslator,
) -> None:
    translator.feed(
        {
            "type": "response.output_item.added",
            "output_index": 0,
            "item": {"type": "function_call", "call_id": "c0", "name": "a"},
        }
    )
    translator.feed(
        {"type": "response.function_call_arguments.delta", "output_index": 0, "delta": "{}"}
    )
    body = _responses_body()
    body["output"] = [
        {"type": "function_call", "call_id": "c0", "name": "a", "arguments": "{}"},
        {"type": "function_call", "call_id": "c1", "name": "b", "arguments": "{}"},
    ]
    tail = translator.feed({"type": "response.completed", "response": body})
    bodies = [_data(line) for line in tail if line.startswith("data: {")]
    tool_chunks = [b for b in bodies if "tool_calls" in b["choices"][0]["delta"]]
    ids = [c["id"] for b in tool_chunks for c in b["choices"][0]["delta"]["tool_calls"]]
    assert ids == ["c1"]


def test_null_id_falls_back() -> None:
    body = _responses_body()
    body["id"] = None
    chat = R.responses_to_chat(body, "m")
    assert chat["id"].startswith("chatcmpl-")


def test_finish_reports_usage_via_callback() -> None:
    recorded: list[dict] = []
    stream_translator = ResponsesStreamTranslator(model="m", on_usage=recorded.append)
    stream_translator.feed({"type": "response.completed", "response": _responses_body()})
    assert recorded == [
        {
            "prompt_tokens": 14,
            "completion_tokens": 60,
            "cached_read_tokens": 0,
            "prompt_chars": 0,
            "completion_chars": 18,
        }
    ]
    assert stream_translator.completion_chars == 18


def test_responses_translator_never_double_counts_terminal_text() -> None:
    """Deltas streamed, terminal repeats the text, so text is counted once.

    The terminal's unseen tool call still counts: 2 (delta) + 16 (name+args).
    """
    recorded: list[dict] = []
    translator = ResponsesStreamTranslator(model="m", on_usage=recorded.append)
    translator.feed({"type": "response.output_text.delta", "delta": "OK"})
    translator.feed({"type": "response.completed", "response": _responses_body()})
    assert translator.completion_chars == 18
    assert recorded[0]["completion_chars"] == 18


def test_native_responses_body_passes_through() -> None:
    adapter = ResponsesAdapter()
    payload = {"model": "m", "input": [{"role": "user", "content": "hi"}]}
    outbound, warnings = adapter.build_request(payload, "upstream-m")
    assert outbound["input"] == [{"role": "user", "content": "hi"}]
    assert outbound["model"] == "upstream-m"
    assert warnings == []


def test_responses_translation_keeps_store() -> None:
    outbound, _ = ResponsesAdapter().build_request(
        {"messages": [{"role": "user", "content": "hi"}], "store": False}, "m"
    )
    assert outbound["store"] is False


def test_verbatim_list_content_warns() -> None:
    warnings = R.translation_warnings(
        {"messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]}
    )
    assert any("verbatim" in w for w in warnings)
    assert R.translation_warnings({"messages": [{"role": "user", "content": "hi"}]}) == []


def test_sse_translator_unseen_tool_call_still_forwarded(
    translator: ResponsesStreamTranslator,
) -> None:
    tail = translator.feed({"type": "response.completed", "response": _responses_body()})
    bodies = [_data(line) for line in tail if line.startswith("data: {")]
    tool_chunks = [body for body in bodies if "tool_calls" in body["choices"][0]["delta"]]
    assert tool_chunks and tool_chunks[0]["choices"][0]["delta"]["tool_calls"][0]["id"] == "call_9"
    assert _data(tail[-2])["choices"][0]["finish_reason"] == "tool_calls"
    assert tail[-1] == "data: [DONE]\n\n"


def test_sse_framing_uses_blank_line_delimiters(
    translator: ResponsesStreamTranslator,
) -> None:
    translator.feed({"type": "response.output_text.delta", "delta": "O"})
    tail = translator.feed({"type": "response.completed", "response": _responses_body()})
    assert tail[-1] == "data: [DONE]\n\n"
    assert all(line.endswith("\n\n") for line in tail if line.startswith("data: "))


def test_finish_reemits_missed_text(translator: ResponsesStreamTranslator) -> None:
    tail = translator.feed(
        {
            "type": "response.completed",
            "response": {
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "OK"}],
                    }
                ],
                "usage": {"input_tokens": 1, "output_tokens": 2, "total_tokens": 3},
            },
        }
    )
    bodies = [_data(line) for line in tail if line.startswith("data: {")]
    assert any(body["choices"][0]["delta"].get("content") == "OK" for body in bodies), tail


def test_dropped_keys_warn(caplog) -> None:  # type: ignore[no-untyped-def]
    with caplog.at_level(
        logging.WARNING, logger="harbor_research_runner.model_proxy.adapters.responses"
    ):
        R.chat_to_responses(
            {
                "messages": [{"role": "user", "content": "hi"}],
                "response_format": {"type": "json_object"},
                "stop": ["###"],
                "tool_choice": {"type": "function", "function": {"name": "bash"}},
            },
            "m",
        )
    assert caplog.text != ""


def test_assistant_list_content_preserved_with_tool_calls() -> None:
    out = R.chat_to_responses(
        {
            "messages": [
                {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "thinking"}],
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "bash", "arguments": "{}"},
                        }
                    ],
                }
            ]
        },
        "m",
    )
    assert "thinking" in json.dumps(out["input"])


def test_assistant_non_text_parts_dropped_not_serialized(caplog) -> None:  # type: ignore[no-untyped-def]
    with caplog.at_level(
        logging.WARNING, logger="harbor_research_runner.model_proxy.adapters.responses"
    ):
        out = R.chat_to_responses(
            {
                "messages": [
                    {
                        "role": "assistant",
                        "content": [
                            {"type": "text", "text": "thinking"},
                            {"type": "image_url", "image_url": {"url": "http://x/y.png"}},
                        ],
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {"name": "bash", "arguments": "{}"},
                            }
                        ],
                    }
                ]
            },
            "m",
        )
    dumped = json.dumps(out["input"])
    assert "thinking" in dumped
    assert "image_url" not in dumped
    assert "http://x/y.png" not in dumped
    assert caplog.text != ""


def test_response_failed_event_records_usage_then_fails() -> None:
    recorded: list[dict] = []
    stream = ResponsesStreamTranslator(model="m", on_usage=recorded.append)
    body = _responses_body()
    body["status"] = "failed"
    with pytest.raises(UpstreamDecodeError, match="failed"):
        stream.feed({"type": "response.failed", "response": body})
    assert stream.finished is True
    assert recorded == [
        {
            "prompt_tokens": 14,
            "completion_tokens": 60,
            "cached_read_tokens": 0,
            "prompt_chars": 0,
            "completion_chars": 0,
        }
    ]


def test_response_failed_event_with_no_usage_still_fails() -> None:
    stream = ResponsesStreamTranslator(model="m")
    with pytest.raises(UpstreamDecodeError):
        stream.feed({"type": "response.failed", "response": {"status": "failed", "usage": None}})
    assert stream.finished is True


def test_response_incomplete_event_is_terminal() -> None:
    stream = ResponsesStreamTranslator(model="m")
    tail = stream.feed(
        {
            "type": "response.incomplete",
            "response": {
                **_responses_body(),
                "status": "incomplete",
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "partial"}],
                    }
                ],
            },
        }
    )
    assert stream.finished is True
    assert tail[-1] == "data: [DONE]\n\n"
    assert _data(tail[-2])["choices"][0]["finish_reason"] == "length"


def test_mid_stream_error_event_fails_the_request() -> None:
    stream = ResponsesStreamTranslator(model="m")
    with pytest.raises(UpstreamDecodeError, match="boom"):
        stream.feed({"type": "error", "error": {"code": "rate_limit", "message": "boom"}})
    assert stream.finished is False


def test_unhandled_event_warns_once_per_translator(caplog) -> None:  # type: ignore[no-untyped-def]
    stream = ResponsesStreamTranslator(model="m")
    with caplog.at_level(
        logging.WARNING, logger="harbor_research_runner.model_proxy.adapters.responses"
    ):
        assert stream.feed({"type": "response.something_new"}) == []
        assert stream.feed({"type": "response.something_new"}) == []
    assert caplog.text.count("response.something_new") == 1


def test_progress_events_are_silent(caplog) -> None:  # type: ignore[no-untyped-def]
    stream = ResponsesStreamTranslator(model="m")
    with caplog.at_level(
        logging.WARNING, logger="harbor_research_runner.model_proxy.adapters.responses"
    ):
        for event in (
            "response.in_progress",
            "response.output_item.done",
            "response.output_text.done",
            "response.content_part.done",
            "response.function_call_arguments.done",
            "response.reasoning_summary_text.delta",
        ):
            assert stream.feed({"type": event}) == []
    assert caplog.text == ""
