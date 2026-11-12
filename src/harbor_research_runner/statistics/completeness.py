from dataclasses import dataclass

from harbor_research_runner.records.trials import TrialRow


@dataclass(frozen=True)
class Completeness:
    slots_planned: int
    slots_complete: int
    slots_incomplete: int
    trials_valid: int
    set_aside_by_reason: dict[str, int]
    trials_unmeasured: int
    set_aside_tokens: int


def completeness(
    rows: list[TrialRow], planned_slots: list[tuple[str, str]], repetitions: int
) -> Completeness:
    final_counts: dict[tuple[str, str], int] = {}
    set_aside_by_reason: dict[str, int] = {}
    set_aside_tokens = 0
    rows = [row for row in rows if row.location != "canary-job"]
    for row in rows:
        if row.is_final:
            key = (row.task, row.arm)
            final_counts[key] = final_counts.get(key, 0) + 1
        if row.location == "set-aside-trials":
            reason = row.reason or "unrecorded"
            set_aside_by_reason[reason] = set_aside_by_reason.get(reason, 0) + 1
            set_aside_tokens += row.prompt_tokens + row.completion_tokens
    slots_complete = sum(final_counts.get(slot, 0) >= repetitions for slot in planned_slots)
    return Completeness(
        slots_planned=len(planned_slots),
        slots_complete=slots_complete,
        slots_incomplete=len(planned_slots) - slots_complete,
        trials_valid=sum(row.valid for row in rows),
        set_aside_by_reason=set_aside_by_reason,
        trials_unmeasured=sum(not row.measured for row in rows),
        set_aside_tokens=set_aside_tokens,
    )
