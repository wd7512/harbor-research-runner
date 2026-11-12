import random
from dataclasses import dataclass

from harbor_research_runner.records.trials import TrialRow

VIEWS = ("first_attempt", "final", "final_excluding_reruns")


def _view_rows(rows: list[TrialRow], view: str) -> list[TrialRow]:
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


@dataclass(frozen=True)
class PairedResult:
    tasks: int
    mean_difference: float | None
    low: float | None
    high: float | None
    unpaired_tasks: int


def paired_difference(
    rows: list[TrialRow],
    view: str,
    arm_a: str,
    arm_b: str,
    resamples: int = 10000,
    seed: int = 0,
) -> PairedResult:
    rewards: dict[str, dict[str, list[float]]] = {}
    for row in _view_rows(rows, view):
        if row.arm in (arm_a, arm_b):
            rewards.setdefault(row.task, {}).setdefault(row.arm, []).append(row.reward or 0.0)
    task_names = sorted(rewards)
    paired_tasks = [
        task for task in task_names if arm_a in rewards[task] and arm_b in rewards[task]
    ]
    unpaired_tasks = len(task_names) - len(paired_tasks)
    if not paired_tasks:
        return PairedResult(0, None, None, None, unpaired_tasks)
    if resamples <= 0:
        raise ValueError("resamples must be positive")
    differences = [
        sum(rewards[task][arm_a]) / len(rewards[task][arm_a])
        - sum(rewards[task][arm_b]) / len(rewards[task][arm_b])
        for task in paired_tasks
    ]
    mean_difference = sum(differences) / len(differences)
    rng = random.Random(seed)
    draws = sorted(
        sum(rng.choice(differences) for _ in differences) / len(differences)
        for _ in range(resamples)
    )
    low = draws[int(0.025 * resamples)]
    high = draws[int(0.975 * resamples)]
    return PairedResult(len(paired_tasks), mean_difference, low, high, unpaired_tasks)
