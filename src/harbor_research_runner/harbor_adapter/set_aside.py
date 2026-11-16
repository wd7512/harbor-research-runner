from __future__ import annotations

import os
from pathlib import Path

from harbor_research_runner.records.jsonl import append_row, read_rows
from harbor_research_runner.records.run_events import SetAside


def repair(run_dir: Path, events_fd: int, now: float) -> list[str]:
    """Write the missing `set_aside` event for a trial moved before a crash.

    The reason, task and arm are copied from the trial's invalid `trial_verdict`
    event, which `run` writes before every set-aside move.
    """
    destinations = run_dir / "set-aside-trials"
    if not destinations.is_dir():
        return []
    recorded = _recorded_trials(run_dir)
    verdicts = _invalid_verdicts(run_dir)
    repaired: list[str] = []
    for trial_dir in sorted(path for path in destinations.iterdir() if path.is_dir()):
        trial = trial_dir.name
        if trial in recorded:
            continue
        verdict = verdicts.get(trial)
        if verdict is None:
            raise ValueError(f"set-aside trial {trial} has no invalid trial_verdict event")
        _append_event(events_fd, trial, verdict["task"], verdict["arm"], verdict["reason"], now)
        recorded.add(trial)
        repaired.append(trial)
    return repaired


def _invalid_verdicts(run_dir: Path) -> dict[str, dict]:
    return {
        row["trial"]: row
        for row in read_rows(run_dir / "run-events.jsonl").rows
        if row.get("type") == "trial_verdict"
        and row.get("valid") is False
        and isinstance(row.get("trial"), str)
        and isinstance(row.get("reason"), str)
    }


def set_aside(
    run_dir: Path,
    trial: str,
    task: str,
    arm: str,
    reason: str,
    events_fd: int,
    now: float,
) -> None:
    source = run_dir / "harbor-job" / trial
    destination = run_dir / "set-aside-trials" / trial
    destination.parent.mkdir(parents=True, exist_ok=True)
    source_exists = source.exists()
    destination_exists = destination.exists()
    if source_exists and destination_exists:
        raise FileExistsError(destination)
    if source_exists:
        os.rename(source, destination)
        _append_event(events_fd, trial, task, arm, reason, now)
    elif destination_exists:
        if trial not in _recorded_trials(run_dir):
            _append_event(events_fd, trial, task, arm, reason, now)
    else:
        os.rename(source, destination)


def set_aside_counts(events: list[dict]) -> dict[tuple[str, str], int]:
    counts: dict[tuple[str, str], int] = {}
    for event in events:
        if event.get("type") != "set_aside":
            continue
        key = (event["task"], event["arm"])
        counts[key] = counts.get(key, 0) + 1
    return counts


def earlier_reasons(events: list[dict], task: str, arm: str) -> tuple[str, ...]:
    return tuple(
        event["reason"]
        for event in events
        if event.get("type") == "set_aside"
        and event.get("task") == task
        and event.get("arm") == arm
    )


def _recorded_trials(run_dir: Path) -> set[str]:
    return {
        row["trial"]
        for row in read_rows(run_dir / "run-events.jsonl").rows
        if row.get("type") == "set_aside" and isinstance(row.get("trial"), str)
    }


def _append_event(events_fd: int, trial: str, task: str, arm: str, reason: str, now: float) -> None:
    append_row(events_fd, SetAside(t=now, trial=trial, task=task, arm=arm, reason=reason).to_row())
