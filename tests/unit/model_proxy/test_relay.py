from __future__ import annotations

from harbor_research_runner.model_proxy.adapters.chat import ChatAdapter


def _translate(raw: bytes):
    translator = ChatAdapter().new_stream_translator("test/model", lambda _usage: None)
    for line in raw.decode("utf-8").splitlines():
        translator.feed_line(line)
    return translator


def test_chat_translator_accepts_crlf_event() -> None:
    translator = _translate(b'data: {"choices":[]}\r\n\r\n')

    assert translator.terminal is False
    assert translator.finished is False


def test_chat_translator_ignores_comment_lines() -> None:
    translator = _translate(b": keepalive\r\n\r\n")

    assert translator.usage is None
    assert translator.terminal is False


def test_chat_translator_joins_multi_data_lines() -> None:
    translator = _translate(b'data: {"model":"m",\r\ndata: "usage":{"prompt_tokens":1}}\r\n\r\n')

    assert translator.served_model == "m"
    assert translator.usage["prompt_tokens"] == 1


def test_chat_translator_recognises_done() -> None:
    translator = _translate(b"data: [DONE]\n\n")

    assert translator.finished is True
    assert translator.terminal is True


def test_finish_reason_is_terminal() -> None:
    translator = _translate(b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n')

    assert translator.terminal is True


def test_terminal_usage_with_empty_choices_is_terminal() -> None:
    translator = _translate(b'data: {"choices":[],"usage":{"prompt_tokens":1}}\n\n')

    assert translator.terminal is True


def test_usage_on_content_is_not_terminal() -> None:
    translator = _translate(
        b'data: {"choices":[{"delta":{"content":"hi"}}],"usage":{"prompt_tokens":1}}\n\n'
    )

    assert translator.terminal is False


def test_usage_on_role_chunk_is_not_terminal() -> None:
    translator = _translate(b'data: {"choices":[{"delta":{"role":"assistant"}}],"usage":{}}\n\n')

    assert translator.terminal is False


def test_ordinary_content_is_not_terminal() -> None:
    translator = _translate(b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n')

    assert translator.terminal is False
