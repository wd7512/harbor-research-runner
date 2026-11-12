from dataclasses import replace

from harbor_research_runner.statistics.usage import label_counts, usage_medians
from tests.unit.statistics.test_rates import _row


def test_usage_medians_uses_valid_trials_for_one_arm():
    rows = [
        replace(_row("a", arm="alpha"), requests=2, prompt_tokens=10, turns=2),
        replace(_row("b", arm="alpha"), requests=4, prompt_tokens=20, turns=4),
        replace(_row("invalid", arm="alpha"), requests=100, valid=False),
        replace(_row("other", arm="beta"), requests=99),
    ]

    assert usage_medians(rows, "alpha") == {
        "requests": 3.0,
        "prompt_tokens": 15.0,
        "completion_tokens": 5.0,
        "prompt_chars": 20.0,
        "completion_chars": 10.0,
        "turns": 3.0,
        "cost_usd_estimate": 0.01,
    }


def test_usage_medians_returns_none_for_empty_or_unmeasured_fields():
    rows = [replace(_row("a", arm="alpha"), turns=None)]

    assert usage_medians(rows, "missing") == {
        "requests": None,
        "prompt_tokens": None,
        "completion_tokens": None,
        "prompt_chars": None,
        "completion_chars": None,
        "turns": None,
        "cost_usd_estimate": None,
    }
    assert usage_medians(rows, "alpha")["turns"] is None


def test_label_counts_counts_each_label_for_one_arm():
    rows = [
        replace(_row("a", arm="alpha"), labels=["timed_out", "possible_bypass"]),
        replace(_row("b", arm="alpha"), labels=["timed_out"]),
        replace(_row("c", arm="beta"), labels=["exit_nonzero"]),
    ]

    assert label_counts(rows, "alpha") == {"possible_bypass": 1, "timed_out": 2}
