"""Tests for the chat API adapter."""

from __future__ import annotations

from harbor_research_runner.model_proxy.adapters.chat import ChatAdapter


def test_chat_adapter_passes_through() -> None:
    adapter = ChatAdapter()
    payload = {"messages": [{"role": "user", "content": "hi"}], "stream": True}
    outbound, warnings = adapter.build_request(payload, "mimo-v2.5")
    assert outbound["messages"] == [{"role": "user", "content": "hi"}]
    assert outbound["model"] == "mimo-v2.5"
    assert warnings == []
    decoded = adapter.decode_response(b'{"ok": true}', "mimo-v2.5", "application/json")
    assert decoded.client_body == b'{"ok": true}'
    assert decoded.upstream_body is None
    assert decoded.usage is None


def test_passthrough_translator_forwards_and_records_usage() -> None:
    recorded: list[dict] = []
    translator = ChatAdapter().new_stream_translator("m", recorded.append)
    assert translator.finished is False
    assert translator.usage is None
    assert translator.feed_line(": ping") == [": ping\n"]
    assert recorded == []
    out = translator.feed_line(
        'data: {"created": 5, "usage": {"prompt_tokens": 3, "completion_tokens": 1}}'
    )
    assert out == ['data: {"created": 5, "usage": {"prompt_tokens": 3, "completion_tokens": 1}}\n']
    assert recorded == [
        {
            "prompt_tokens": 3,
            "completion_tokens": 1,
            "cached_read_tokens": 0,
            "prompt_chars": 0,
            "completion_chars": 0,
        }
    ]


def test_passthrough_translator_accumulates_completion_chars() -> None:
    """streamed content deltas accumulate as completion chars."""
    translator = ChatAdapter().new_stream_translator("m", lambda usage: None)
    translator.feed_line('data: {"choices": [{"delta": {"content": "hé"}}]}')
    translator.feed_line('data: {"choices": [{"delta": {"content": "llo"}}]}')
    assert translator.completion_chars == 5
