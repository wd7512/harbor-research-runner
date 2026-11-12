from dataclasses import replace

from harbor_research_runner.statistics.completeness import completeness
from tests.unit.statistics.test_rates import _row


def test_completeness_reports_set_aside_tokens():
    rows = [
        _row(
            "set-aside", "task-a", "arm-a", "set-aside-trials", False, False, 0.0, reason="upstream"
        ),
        _row("final-a1", "task-a", "arm-a", first=False, reward=1.0),
        _row("final-a2", "task-a", "arm-a", first=False, reward=0.0),
        _row("final-b1", "task-a", "arm-b", reward=1.0),
        _row("final-b2", "task-a", "arm-b", first=False, reward=0.0),
        _row("valid-unmeasured", "task-b", "arm-a", reward=1.0),
    ]
    rows[-1] = replace(rows[-1], measured=False)

    result = completeness(rows, [("task-a", "arm-a"), ("task-a", "arm-b"), ("task-b", "arm-a")], 2)

    assert result.slots_planned == 3
    assert result.slots_complete == 2
    assert result.slots_incomplete == 1
    assert result.trials_valid == 5
    assert result.set_aside_by_reason == {"upstream": 1}
    assert result.trials_unmeasured == 1
    assert result.set_aside_tokens == 15


def test_completeness_never_counts_canary_trials():
    canary = replace(_row("canary", "say-hello", "arm-a", "canary-job", reward=1.0), measured=False)
    rows = [_row("final-a1", "task-a", "arm-a", first=False, reward=1.0), canary]

    result = completeness(rows, [("task-a", "arm-a")], 1)

    assert result.trials_valid == 1
    assert result.trials_unmeasured == 0
