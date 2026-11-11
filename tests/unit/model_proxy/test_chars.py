from __future__ import annotations

import json

from harbor_research_runner.model_proxy import chars


def test_prompt_chars_counts_message_text() -> None:
    payload = {"messages": [{"role": "user", "content": "hello"}]}
    assert chars.prompt_chars_of_payload(payload) == 5


def test_prompt_chars_counts_characters_not_bytes() -> None:
    payload = {"messages": [{"role": "user", "content": "café \U0001f980"}]}
    assert chars.prompt_chars_of_payload(payload) == 6


def test_prompt_chars_covers_responses_input_shape() -> None:
    payload = {"input": [{"role": "user", "content": "hello"}]}
    assert chars.prompt_chars_of_payload(payload) == 5


def test_prompt_chars_includes_tool_definitions() -> None:
    tools = [{"type": "function", "name": "f", "parameters": {}}]
    payload = {"messages": [{"role": "user", "content": "hi"}], "tools": tools}
    assert chars.prompt_chars_of_payload(payload) == 2 + len(json.dumps(tools))


def test_chat_completion_chars_counts_text_and_tool_arguments() -> None:
    body = {
        "choices": [
            {
                "message": {
                    "content": "ok",
                    "tool_calls": [{"function": {"name": "f", "arguments": '{"a":1}'}}],
                }
            }
        ]
    }
    assert chars.chat_completion_chars(body) == 2 + 1 + len('{"a":1}')


def test_chat_completion_chars_counts_characters_not_bytes() -> None:
    body = {"choices": [{"message": {"content": "café \U0001f980"}}]}
    assert chars.chat_completion_chars(body) == 6


def test_tool_call_text_is_counted() -> None:
    delta = {
        "choices": [
            {
                "delta": {
                    "tool_calls": [{"function": {"name": "lookup", "arguments": '{"query":"x"}'}}]
                }
            }
        ]
    }
    assert chars.chat_delta_chars(delta) == len("lookup") + len('{"query":"x"}')
