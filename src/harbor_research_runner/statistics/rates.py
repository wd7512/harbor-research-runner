import math
from dataclasses import dataclass

from harbor_research_runner.records.trials import TrialRow

VIEWS = ("first_attempt", "final", "final_excluding_reruns")


@dataclass(frozen=True)
class ArmRate:
    passes: int
    trials: int
    rate: float
    low: float
    high: float


@dataclass(frozen=True)
class TimeBucket:
    start_t: float
    passes: int
    trials: int
    rate: float | None


def trials_in_view(rows: list[TrialRow], view: str) -> list[TrialRow]:
    if view not in VIEWS:
        raise ValueError(f"unknown statistics view: {view}")
    non_canary = [row for row in rows if row.location != "canary-job"]
    if view == "first_attempt":
        return [row for row in non_canary if row.is_first_attempt]
    final_rows = [row for row in non_canary if row.is_final]
    if view == "final":
        return final_rows
    rerun_slots = {(row.task, row.arm) for row in non_canary if row.location == "set-aside-trials"}
    return [row for row in final_rows if (row.task, row.arm) not in rerun_slots]


def wilson_interval(passes: int, n: int) -> tuple[float, float]:
    if n == 0:
        return (0.0, 0.0)
    z = 1.959963984540054
    p = passes / n
    denominator = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denominator
    margin = z * math.sqrt((p * (1 - p) + z * z / (4 * n)) / n) / denominator
    return (center - margin, center + margin)


def arm_rates(rows: list[TrialRow], view: str) -> dict[str, ArmRate]:
    by_arm: dict[str, list[TrialRow]] = {}
    for row in trials_in_view(rows, view):
        by_arm.setdefault(row.arm, []).append(row)
    result = {}
    for arm, arm_rows in by_arm.items():
        passes = sum(row.reward == 1.0 for row in arm_rows)
        trials = len(arm_rows)
        low, high = wilson_interval(passes, trials)
        result[arm] = ArmRate(passes, trials, passes / trials, low, high)
    return result


def task_arm_table(rows: list[TrialRow], view: str) -> dict[tuple[str, str], tuple[int, int]]:
    counts: dict[tuple[str, str], list[int]] = {}
    for row in trials_in_view(rows, view):
        key = (row.task, row.arm)
        if key not in counts:
            counts[key] = [0, 0]
        counts[key][0] += row.reward == 1.0
        counts[key][1] += 1
    return {key: (values[0], values[1]) for key, values in counts.items()}


def pass_rate_over_time(rows: list[TrialRow], bucket_s: int = 7200) -> list[TimeBucket]:
    if bucket_s <= 0:
        raise ValueError("bucket_s must be positive")
    included = _timed_first_attempts(rows)
    if not included:
        return []
    first_t = min(start_t for _, start_t in included)
    return _bucket_over_time(included, bucket_s, first_t)


def pass_rate_over_time_by_arm(
    rows: list[TrialRow], bucket_s: int = 7200
) -> dict[str, list[TimeBucket]]:
    if bucket_s <= 0:
        raise ValueError("bucket_s must be positive")
    included = _timed_first_attempts(rows)
    arms = sorted({row.arm for row in trials_in_view(rows, "first_attempt")})
    if not included:
        return {arm: [] for arm in arms}
    origin_t = min(start_t for _, start_t in included)
    shared_bucket_count = len(_bucket_over_time(included, bucket_s, origin_t))
    return {
        arm: _bucket_over_time(
            [(row, start_t) for row, start_t in included if row.arm == arm],
            bucket_s,
            origin_t,
            shared_bucket_count,
        )
        for arm in arms
    }


def _timed_first_attempts(rows: list[TrialRow]) -> list[tuple[TrialRow, float]]:
    return [
        (row, row.agent_started_t)
        for row in trials_in_view(rows, "first_attempt")
        if row.agent_started_t is not None
    ]


def _bucket_over_time(
    included: list[tuple[TrialRow, float]],
    bucket_s: int,
    origin_t: float,
    bucket_count: int | None = None,
) -> list[TimeBucket]:
    buckets: dict[int, list[int]] = {}
    for row, start_t in included:
        index = int((start_t - origin_t) // bucket_s)
        counts = buckets.setdefault(index, [0, 0])
        counts[0] += row.reward == 1.0
        counts[1] += 1
    count = bucket_count if bucket_count is not None else max(buckets, default=-1) + 1
    return [
        _time_bucket(origin_t + index * bucket_s, *buckets.get(index, [0, 0]))
        for index in range(count)
    ]


def _time_bucket(start_t: float, passes: int, trials: int) -> TimeBucket:
    return TimeBucket(start_t, passes, trials, passes / trials if trials else None)
