import pytest

from harbor_research_runner.records.trials import TrialRow
from harbor_research_runner.statistics.rates import (
    arm_rates,
    pass_rate_over_time,
    pass_rate_over_time_by_arm,
    task_arm_table,
    trials_in_view,
    wilson_interval,
)


def _row(
    trial: str,
    task: str = "task-a",
    arm: str = "arm-a",
    location: str = "harbor-job",
    first: bool = True,
    final: bool = True,
    reward: float | None = 1.0,
    started: float | None = 100.0,
    reason: str | None = None,
) -> TrialRow:
    return TrialRow(
        trial=trial,
        task=task,
        arm=arm,
        harness="opencode",
        commit="c" * 40,
        model="provider/model",
        location=location,
        is_first_attempt=first,
        is_final=final,
        reward=reward,
        is_pass=reward == 1.0,
        harbor_exception=None,
        agent_exit_nonzero=False,
        environment_setup_started_t=started,
        environment_setup_finished_t=started,
        agent_setup_started_t=started,
        agent_setup_finished_t=started,
        agent_started_t=started,
        agent_finished_t=started,
        verifier_started_t=started,
        verifier_finished_t=started,
        valid=final,
        reason=reason,
        instrument=None,
        labels=[],
        measured=True,
        unmeasured_reason=None,
        requests=1,
        error_requests=0,
        refused_requests=0,
        prompt_tokens=10,
        completion_tokens=5,
        cached_read_tokens=0,
        prompt_chars=20,
        completion_chars=10,
        cost_usd_estimate=0.01,
        harbor_reported_tokens=15,
        turns=2,
        tool_calls=1,
        median_first_content_s=0.2,
        output_rate_tokens_per_s=5.0,
        agent_wall_s=1.0,
    )


@pytest.mark.parametrize(
    ("passes", "n", "expected"),
    [
        (0, 10, (0.0, 0.2775328)),
        (10, 10, (0.7224672, 1.0)),
        (5, 10, (0.2365931, 0.7634069)),
        (1, 1, (0.2065493, 1.0)),
        (0, 0, (0.0, 0.0)),
    ],
)
def test_wilson_interval_matches_reference_values(passes, n, expected):
    assert wilson_interval(passes, n) == pytest.approx(expected, abs=1e-7)


def test_views_exclude_set_aside_slots_from_final_excluding_reruns():
    rows = [
        _row("a1", "task-a", "arm-a", "set-aside-trials", True, False, 0.0, reason="bad"),
        _row("a2", "task-a", "arm-a", first=True, reward=1.0),
        _row("a3", "task-a", "arm-a", first=False, reward=0.0),
        _row("b1", "task-a", "arm-b", reward=0.0),
        _row("b2", "task-a", "arm-b", reward=1.0),
        _row("c1", "task-b", "arm-a", reward=1.0),
        _row("c2", "task-b", "arm-a", reward=None),
        _row("d1", "task-b", "arm-b", reward=0.0),
        _row("d2", "task-b", "arm-b", reward=1.0),
        _row("canary", "canary", "arm-a", "canary-job", reward=1.0),
    ]

    assert [row.trial for row in trials_in_view(rows, "first_attempt")] == [
        "a1",
        "a2",
        "b1",
        "b2",
        "c1",
        "c2",
        "d1",
        "d2",
    ]
    assert [row.trial for row in trials_in_view(rows, "final")] == [
        "a2",
        "a3",
        "b1",
        "b2",
        "c1",
        "c2",
        "d1",
        "d2",
    ]
    assert [row.trial for row in trials_in_view(rows, "final_excluding_reruns")] == [
        "b1",
        "b2",
        "c1",
        "c2",
        "d1",
        "d2",
    ]
    assert arm_rates(rows, "first_attempt")["arm-a"].passes == 2
    assert task_arm_table(rows, "final")[("task-b", "arm-a")] == (1, 2)


@pytest.mark.parametrize("view", ["first_attempt", "final", "final_excluding_reruns"])
def test_canary_trials_never_appear_in_a_view(view):
    rows = [_row("regular"), _row("canary", location="canary-job")]

    assert [row.trial for row in trials_in_view(rows, view)] == ["regular"]


def test_pass_rate_over_time_emits_empty_buckets_and_omits_missing_times():
    rows = [
        _row("first", started=100.0, reward=1.0),
        _row("last", started=310.0, reward=0.0),
        _row("missing", started=None, reward=1.0),
        _row("canary", location="canary-job", started=110.0),
    ]

    assert [
        (bucket.start_t, bucket.passes, bucket.trials)
        for bucket in pass_rate_over_time(rows, bucket_s=100)
    ] == [(100.0, 1, 1), (200.0, 0, 0), (300.0, 0, 1)]


def test_pass_rate_over_time_rate_is_passes_over_trials_and_none_for_an_empty_bucket():
    rows = [
        _row("pass", started=100.0, reward=1.0),
        _row("fail", started=120.0, reward=0.0),
        _row("late", started=310.0, reward=1.0),
    ]

    assert [bucket.rate for bucket in pass_rate_over_time(rows, bucket_s=100)] == [0.5, None, 1.0]


def test_pass_rate_over_time_by_arm_aligns_buckets_to_a_shared_origin():
    rows = [
        _row("a-first", arm="arm-a", started=100.0, reward=1.0),
        _row("b-first", arm="arm-b", started=250.0, reward=0.0),
        _row("a-last", arm="arm-a", started=310.0, reward=0.0),
    ]

    result = pass_rate_over_time_by_arm(rows, bucket_s=100)

    assert {
        arm: [(bucket.start_t, bucket.passes, bucket.trials) for bucket in buckets]
        for arm, buckets in result.items()
    } == {
        "arm-a": [(100.0, 1, 1), (200.0, 0, 0), (300.0, 0, 1)],
        "arm-b": [(100.0, 0, 0), (200.0, 0, 1), (300.0, 0, 0)],
    }
