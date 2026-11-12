from statistics import median

from harbor_research_runner.records.trials import TrialRow

USAGE_FIELDS = (
    "requests",
    "prompt_tokens",
    "completion_tokens",
    "prompt_chars",
    "completion_chars",
    "turns",
    "cost_usd_estimate",
)


def usage_medians(rows: list[TrialRow], arm: str) -> dict[str, float | None]:
    arm_rows = [row for row in rows if row.arm == arm and row.valid]
    result: dict[str, float | None] = {}
    for field in USAGE_FIELDS:
        values = [getattr(row, field) for row in arm_rows if getattr(row, field) is not None]
        result[field] = float(median(values)) if values else None
    return result


def label_counts(rows: list[TrialRow], arm: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        if row.arm == arm:
            for label in row.labels:
                counts[label] = counts.get(label, 0) + 1
    return dict(sorted(counts.items()))
