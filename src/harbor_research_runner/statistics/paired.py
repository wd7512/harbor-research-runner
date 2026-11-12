import random
from dataclasses import dataclass

from harbor_research_runner.records.trials import TrialRow
from harbor_research_runner.statistics.rates import trials_in_view


@dataclass(frozen=True)
class TaskDifference:
    task: str
    rate_a: float
    rate_b: float
    difference: float


@dataclass(frozen=True)
class PairedResult:
    tasks: int
    mean_difference: float | None
    low: float | None
    high: float | None
    unpaired_tasks: int
    per_task: tuple[TaskDifference, ...]


def paired_difference(
    rows: list[TrialRow],
    view: str,
    arm_a: str,
    arm_b: str,
    resamples: int = 10000,
    seed: int = 0,
) -> PairedResult:
    rewards: dict[str, dict[str, list[float]]] = {}
    for row in trials_in_view(rows, view):
        if row.arm in (arm_a, arm_b):
            rewards.setdefault(row.task, {}).setdefault(row.arm, []).append(row.reward or 0.0)
    task_names = sorted(rewards)
    paired_tasks = [
        task for task in task_names if arm_a in rewards[task] and arm_b in rewards[task]
    ]
    unpaired_tasks = len(task_names) - len(paired_tasks)
    if not paired_tasks:
        return PairedResult(0, None, None, None, unpaired_tasks, ())
    if resamples <= 0:
        raise ValueError("resamples must be positive")
    per_task = []
    for task in paired_tasks:
        rate_a = sum(rewards[task][arm_a]) / len(rewards[task][arm_a])
        rate_b = sum(rewards[task][arm_b]) / len(rewards[task][arm_b])
        per_task.append(TaskDifference(task, rate_a, rate_b, rate_a - rate_b))
    differences = [item.difference for item in per_task]
    mean_difference = sum(differences) / len(differences)
    rng = random.Random(seed)
    draws = sorted(
        sum(rng.choice(differences) for _ in differences) / len(differences)
        for _ in range(resamples)
    )
    low = draws[int(0.025 * resamples)]
    high = draws[int(0.975 * resamples)]
    return PairedResult(
        len(paired_tasks), mean_difference, low, high, unpaired_tasks, tuple(per_task)
    )
