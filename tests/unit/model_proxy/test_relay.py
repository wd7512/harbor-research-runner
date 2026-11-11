from __future__ import annotations

from harbor_research_runner.model_proxy.relay import _is_terminal, _parse_event


def test_parse_event_reads_crlf_event_and_keeps_raw_bytes() -> None:
    raw = b'data: {"choices":[]}\r\n\r\n'
    parsed_raw, obj, done = _parse_event(raw)

    assert parsed_raw == raw
    assert obj == {"choices": []}
    assert done is False


def test_parse_event_ignores_comment_lines() -> None:
    raw = b": keepalive\r\n\r\n"

    assert _parse_event(raw) == (raw, None, False)


def test_parse_event_joins_multi_data_lines() -> None:
    raw = b'data: {"model":"m",\r\ndata: "usage":{"prompt_tokens":1}}\r\n\r\n'
    parsed_raw, obj, done = _parse_event(raw)

    assert parsed_raw == raw
    assert obj == {"model": "m", "usage": {"prompt_tokens": 1}}
    assert done is False


def test_parse_event_recognises_done() -> None:
    raw = b"data: [DONE]\n\n"

    assert _parse_event(raw) == (raw, None, True)


def test_finish_reason_is_terminal() -> None:
    assert _is_terminal({"choices": [{"delta": {}, "finish_reason": "stop"}]}) is True


def test_terminal_usage_with_empty_choices_is_terminal() -> None:
    assert _is_terminal({"choices": [], "usage": {"prompt_tokens": 1}}) is True


def test_usage_on_content_is_not_terminal() -> None:
    assert (
        _is_terminal(
            {
                "choices": [{"delta": {"content": "hi"}}],
                "usage": {"prompt_tokens": 1},
            }
        )
        is False
    )


def test_usage_on_role_chunk_is_not_terminal() -> None:
    assert (
        _is_terminal(
            {
                "choices": [{"delta": {"role": "assistant"}}],
                "usage": {},
            }
        )
        is False
    )


def test_ordinary_content_is_not_terminal() -> None:
    assert _is_terminal({"choices": [{"delta": {"content": "hi"}}]}) is False
