"""Tests for the model proxy adapter interface."""

from __future__ import annotations

import pytest

from harbor_research_runner.model_proxy.adapters.base import UpstreamDecodeError, normalize_usage
from harbor_research_runner.model_proxy.adapters.responses import ResponsesAdapter


def test_normalize_usage_returns_none_without_usage() -> None:
    assert normalize_usage({}) is None
    assert normalize_usage({"usage": "bad"}) is None
    assert normalize_usage({"response": {}}) is None


def test_normalize_usage_direct_tokens_with_cached() -> None:
    assert normalize_usage(
        {
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 4,
                "prompt_tokens_details": {"cached_tokens": 3},
            }
        }
    ) == {
        "prompt_tokens": 10,
        "completion_tokens": 4,
        "cached_read_tokens": 3,
        "prompt_chars": 0,
        "completion_chars": 0,
    }


def test_decode_failure_raises_upstream_error() -> None:
    with pytest.raises(UpstreamDecodeError):
        ResponsesAdapter().decode_response(b"not-json", "m", "application/json")
