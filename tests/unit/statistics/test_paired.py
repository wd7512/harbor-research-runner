from harbor_research_runner.records.trials import TrialRow
from harbor_research_runner.statistics.paired import paired_difference
from tests.unit.statistics.test_rates import _row


def test_bootstrap_is_deterministic_for_a_fixed_seed():
    rows = [
        _row("a1", "task-a", "arm-a", reward=1.0),
        _row("a2", "task-a", "arm-b", reward=0.0),
        _row("b1", "task-b", "arm-a", reward=None),
        _row("b2", "task-b", "arm-b", reward=0.0),
        _row("c1", "task-c", "arm-a", reward=1.0),
        _row("c2", "task-c", "arm-b", reward=0.0),
    ]

    first = paired_difference(rows, "final", "arm-a", "arm-b", resamples=500, seed=7)
    second = paired_difference(rows, "final", "arm-a", "arm-b", resamples=500, seed=7)

    assert first == second
    assert first.tasks == 3
    assert first.mean_difference == 2 / 3
    assert first.low <= first.mean_difference <= first.high
    assert 0.0 <= first.low <= first.high <= 1.0


def test_bootstrap_interval_collapses_when_task_differences_are_equal():
    rows = [
        _row("a1", "task-a", "arm-a", reward=1.0),
        _row("a2", "task-a", "arm-b", reward=0.0),
        _row("b1", "task-b", "arm-a", reward=1.0),
        _row("b2", "task-b", "arm-b", reward=0.0),
    ]

    result = paired_difference(rows, "final", "arm-a", "arm-b", resamples=100, seed=11)

    assert result.low == result.high == result.mean_difference == 1.0


def test_tasks_missing_an_arm_are_counted_as_unpaired():
    rows = [
        _row("a1", "task-a", "arm-a"),
        _row("b1", "task-b", "arm-b"),
        _row("c1", "task-c", "arm-a"),
        _row("c2", "task-c", "arm-b"),
    ]

    result = paired_difference(rows, "final", "arm-a", "arm-b", resamples=50)

    assert result.tasks == 1
    assert result.unpaired_tasks == 2


def test_no_paired_tasks_has_no_estimate_or_interval():
    rows: list[TrialRow] = [_row("a1", "task-a", "arm-a")]

    result = paired_difference(rows, "final", "arm-a", "arm-b", resamples=5)

    assert (result.tasks, result.mean_difference, result.low, result.high) == (0, None, None, None)
