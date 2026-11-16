import pytest

from harbor_research_runner.statistics.cost import cost_usd


def test_cost_prices_cached_tokens_at_the_cache_rate_only():
    pricing = {"price_in": 2.0, "price_cache": 0.5, "price_out": 4.0}

    assert cost_usd(
        pricing, prompt_tokens=100, completion_tokens=20, cached_read_tokens=40
    ) == pytest.approx(0.00022)


def test_cost_of_zero_tokens_is_zero():
    pricing = {"price_in": 2.0, "price_cache": 0.5, "price_out": 4.0}

    assert cost_usd(pricing, 0, 0, 0) == 0.0


def test_cost_refuses_more_cached_tokens_than_prompt_tokens():
    pricing = {"price_in": 2.0, "price_cache": 0.5, "price_out": 4.0}

    with pytest.raises(ValueError):
        cost_usd(pricing, prompt_tokens=2, completion_tokens=0, cached_read_tokens=3)
