from harbor_research_runner.model_proxy.adapters.responses import ResponsesAdapter
from harbor_research_runner.model_proxy.shaping import shape_request


def test_groq_chat_drops_configured_message_fields_and_clamps_token_limits():
    body = {
        "model": "groq/model",
        "messages": [
            {"role": "user", "content": "question"},
            {"role": "assistant", "content": "answer", "reasoning_content": "thinking"},
        ],
        "max_tokens": 999_999,
        "max_completion_tokens": 16_384,
        "temperature": 0.2,
    }
    card = {
        "request": {
            "drop_message_fields": ["reasoning_content"],
            "max_tokens_cap": 128,
        }
    }

    shaped = shape_request(body, card)

    assert shaped == {
        "model": "groq/model",
        "messages": [
            {"role": "user", "content": "question"},
            {"role": "assistant", "content": "answer"},
        ],
        "max_tokens": 128,
        "max_completion_tokens": 128,
        "temperature": 0.2,
    }
    assert body["messages"][1]["reasoning_content"] == "thinking"
    assert body["max_tokens"] == 999_999


def test_request_shaping_leaves_payload_unchanged_when_request_is_absent():
    body = {"messages": [{"role": "assistant", "reasoning_content": "keep"}], "max_tokens": 999_999}

    assert shape_request(body, {}) == body


def test_request_shaping_leaves_non_message_shapes_unchanged():
    body = {"messages": "not-a-list"}

    assert shape_request(body, {"request": {"drop_message_fields": ["reasoning_content"]}}) == body


def test_request_shaping_clamps_responses_output_token_field():
    outbound, _ = ResponsesAdapter().build_request(
        {"messages": [{"role": "user", "content": "question"}], "max_tokens": 999_999},
        "wire-model",
    )

    assert outbound["max_output_tokens"] == 999_999
    assert shape_request(outbound, {"request": {"max_tokens_cap": 128}}) == {
        "model": "wire-model",
        "input": [{"role": "user", "content": "question"}],
        "max_output_tokens": 128,
    }


def test_request_shaping_leaves_responses_adapter_output_unclamped_without_card_request():
    outbound, _ = ResponsesAdapter().build_request(
        {"messages": [{"role": "user", "content": "question"}], "max_tokens": 999_999},
        "wire-model",
    )

    assert shape_request(outbound, {}) == outbound


def test_request_shaping_clamps_each_supported_token_field_only_above_the_cap():
    for key in ("max_tokens", "max_completion_tokens", "max_output_tokens"):
        assert shape_request({key: 129}, {"request": {"max_tokens_cap": 128}}) == {key: 128}
        assert shape_request({key: 128}, {"request": {"max_tokens_cap": 128}}) == {key: 128}
        assert shape_request({key: 100}, {"request": {"max_tokens_cap": 128}}) == {key: 100}
    assert shape_request({"max_tokens": 999}, {"request": {"max_tokens_cap": None}}) == {
        "max_tokens": 999
    }


def test_request_shaping_passes_non_integer_token_fields_through():
    for key in ("max_tokens", "max_completion_tokens", "max_output_tokens"):
        for value in ("999", 999.0, True, False, None, [999]):
            body = {key: value}
            assert shape_request(body, {"request": {"max_tokens_cap": 128}}) == body
