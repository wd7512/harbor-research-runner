from __future__ import annotations

import pytest

from harbor_research_runner.model_proxy import usage

_CANONICAL = {
    "prompt_tokens": 10,
    "completion_tokens": 4,
    "cached_read_tokens": 3,
    "prompt_chars": 0,
    "completion_chars": 0,
}


def _chat_envelope() -> dict:
    return {
        "usage": {
            "prompt_tokens": 10,
            "completion_tokens": 4,
            "prompt_tokens_details": {"cached_tokens": 3},
        }
    }


def _responses_envelope() -> dict:
    return {
        "usage": {
            "input_tokens": 10,
            "output_tokens": 4,
            "input_tokens_details": {"cached_tokens": 3},
        }
    }


def test_normalize_usage_chat_shape() -> None:
    assert usage.normalize_usage(_chat_envelope()) == _CANONICAL


def test_normalize_usage_responses_shape() -> None:
    assert usage.normalize_usage(_responses_envelope()) == _CANONICAL


def test_normalize_usage_canonical_keys_win() -> None:
    assert usage.normalize_usage(
        {"usage": {"prompt_tokens": 10, "input_tokens": 99, "completion_tokens": 4}}
    ) == {
        "prompt_tokens": 10,
        "completion_tokens": 4,
        "cached_read_tokens": 0,
        "prompt_chars": 0,
        "completion_chars": 0,
    }


def test_normalize_usage_nested_response_shape() -> None:
    assert usage.normalize_usage(
        {"response": {"usage": {"input_tokens": 7, "output_tokens": 3}}}
    ) == {
        "prompt_tokens": 7,
        "completion_tokens": 3,
        "cached_read_tokens": 0,
        "prompt_chars": 0,
        "completion_chars": 0,
    }


@pytest.mark.parametrize("obj", [{}, {"usage": "bad"}, {"response": {}}, [], None])
def test_normalize_usage_absent_is_none(obj: object) -> None:
    assert usage.normalize_usage(obj) is None


@pytest.mark.parametrize("raw", [{}, "bad", None, 0])
def test_normalize_usage_dict_garbage_is_zeros(raw: object) -> None:
    assert usage.normalize_usage_dict(raw) == {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "cached_read_tokens": 0,
        "prompt_chars": 0,
        "completion_chars": 0,
    }


def test_normalize_usage_dict_coerces_values() -> None:
    assert usage.normalize_usage_dict(
        {"prompt_tokens": None, "completion_tokens": "7", "cached_read_tokens": "bad"}
    ) == {
        "prompt_tokens": 0,
        "completion_tokens": 7,
        "cached_read_tokens": 0,
        "prompt_chars": 0,
        "completion_chars": 0,
    }


def test_normalize_usage_dict_preserves_char_counts() -> None:
    assert usage.normalize_usage_dict({"prompt_chars": 12, "completion_chars": "5"}) == {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "cached_read_tokens": 0,
        "prompt_chars": 12,
        "completion_chars": 5,
    }


def test_normalize_usage_dict_infinite_value_is_zero(caplog) -> None:
    with caplog.at_level("WARNING", logger="harbor_research_runner.model_proxy.usage"):
        result = usage.normalize_usage_dict({"prompt_tokens": float("inf")})

    assert result["prompt_tokens"] == 0
    assert "non-numeric usage value" in caplog.text


def test_normalize_usage_dict_cache_read_alias() -> None:
    assert usage.normalize_usage_dict({"cache_read_tokens": 5})["cached_read_tokens"] == 5


def test_normalize_usage_dict_top_level_cached_wins_over_details() -> None:
    assert (
        usage.normalize_usage_dict(
            {
                "cached_read_tokens": 5,
                "prompt_tokens_details": {"cached_tokens": 99},
            }
        )["cached_read_tokens"]
        == 5
    )


def test_normalize_usage_dict_is_idempotent() -> None:
    once = usage.normalize_usage(_chat_envelope())
    assert once is not None
    assert usage.normalize_usage_dict(once) == once
