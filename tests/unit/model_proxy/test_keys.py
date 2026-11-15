from __future__ import annotations

import pytest

from harbor_research_runner.model_proxy.keys import KeyProvider


def test_routes_round_robin_over_two_keys() -> None:
    provider = KeyProvider(
        {"endpoints": ["https://upstream.test/v1/chat/completions"], "key_env": ["KEY_A", "KEY_B"]},
        {"KEY_A": "sk-test-a", "KEY_B": "sk-test-b"},
    )

    first = list(provider.routes())
    second = list(provider.routes())

    assert [(route.endpoint, route.key) for route in first] == [
        ("https://upstream.test/v1/chat/completions", "sk-test-a"),
        ("https://upstream.test/v1/chat/completions", "sk-test-b"),
    ]
    assert [(route.endpoint, route.key) for route in second] == [
        ("https://upstream.test/v1/chat/completions", "sk-test-b"),
        ("https://upstream.test/v1/chat/completions", "sk-test-a"),
    ]


def test_routes_visit_each_endpoint_key_pair_once() -> None:
    provider = KeyProvider(
        {"endpoints": ["https://one.test", "https://two.test"], "key_env": ["KEY_A", "KEY_B"]},
        {"KEY_A": "sk-test-a", "KEY_B": "sk-test-b"},
    )

    routes = list(provider.routes())

    assert [(route.endpoint, route.key) for route in routes] == [
        ("https://one.test", "sk-test-a"),
        ("https://one.test", "sk-test-b"),
        ("https://two.test", "sk-test-a"),
        ("https://two.test", "sk-test-b"),
    ]


def test_single_key_has_one_route() -> None:
    provider = KeyProvider(
        {"endpoints": ["https://upstream.test"], "key_env": ["KEY_A"]},
        {"KEY_A": "sk-test-a"},
    )

    assert [(route.endpoint, route.key) for route in provider.routes()] == [
        ("https://upstream.test", "sk-test-a")
    ]


def test_missing_key_variable_is_named_without_exposing_key_values() -> None:
    with pytest.raises(KeyError) as error:
        KeyProvider(
            {"endpoints": ["https://upstream.test"], "key_env": ["MISSING_KEY"]},
            {"PRESENT_KEY": "sk-test-a"},
        )

    assert "MISSING_KEY" in str(error.value)
    assert "sk-test-a" not in str(error.value)
