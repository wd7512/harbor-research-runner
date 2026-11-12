from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from harbor_research_runner.harbor_adapter.set_aside import (
    earlier_reasons,
    repair,
    set_aside,
    set_aside_counts,
)
from harbor_research_runner.records.jsonl import read_rows


def _write_trial(trial_dir: Path, *, task: str = "task-a", arm: str = "arm-a") -> None:
    trial_dir.mkdir(parents=True)
    (trial_dir / "config.json").write_text(
        json.dumps(
            {
                "task": {"name": task},
                "trial_name": trial_dir.name,
                "agent": {"kwargs": {"arm_label": arm}},
            }
        ),
        encoding="utf-8",
    )


def _events_fd(run_dir: Path) -> int:
    return os.open(run_dir / "run-events.jsonl", os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)


def test_set_aside_destination_is_inside_run_folder(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    source = run_dir / "harbor-job" / "trial-a"
    _write_trial(source)
    fd = _events_fd(run_dir)

    try:
        set_aside(run_dir, "trial-a", "task-a", "arm-a", "invalid", fd, 12.5)
    finally:
        os.close(fd)

    assert (run_dir / "set-aside-trials" / "trial-a").is_dir()
    assert not source.exists()
    assert (run_dir / "set-aside-trials" / "trial-a").is_relative_to(run_dir)
    assert read_rows(run_dir / "run-events.jsonl").rows == [
        {
            "arm": "arm-a",
            "reason": "invalid",
            "t": 12.5,
            "task": "task-a",
            "trial": "trial-a",
            "type": "set_aside",
        }
    ]


def test_set_aside_twice_leaves_one_folder_and_event(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    _write_trial(run_dir / "harbor-job" / "trial-a")
    fd = _events_fd(run_dir)

    try:
        set_aside(run_dir, "trial-a", "task-a", "arm-a", "invalid", fd, 1.0)
        set_aside(run_dir, "trial-a", "task-a", "arm-a", "invalid", fd, 2.0)
    finally:
        os.close(fd)

    assert sorted(path.name for path in (run_dir / "set-aside-trials").iterdir()) == ["trial-a"]
    assert len(read_rows(run_dir / "run-events.jsonl").rows) == 1


def test_set_aside_existing_destination_writes_missing_event(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    _write_trial(run_dir / "set-aside-trials" / "trial-a")
    fd = _events_fd(run_dir)

    try:
        set_aside(run_dir, "trial-a", "task-a", "arm-a", "invalid", fd, 2.0)
    finally:
        os.close(fd)

    assert (run_dir / "set-aside-trials" / "trial-a").is_dir()
    assert read_rows(run_dir / "run-events.jsonl").rows[0]["reason"] == "invalid"


def _verdict_row(trial: str, reason: str) -> dict:
    return {
        "arm": "arm-a",
        "instrument": "harbor",
        "labels": [],
        "measured": True,
        "reason": reason,
        "t": 2.0,
        "task": "task-a",
        "trial": trial,
        "type": "trial_verdict",
        "unmeasured_reason": None,
        "valid": False,
    }


def test_set_aside_interrupted_between_move_and_event_is_repaired(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    destination = run_dir / "set-aside-trials" / "trial-a"
    _write_trial(destination)
    verdict = _verdict_row("trial-a", "harness_setup_failed")
    (run_dir / "run-events.jsonl").write_text(json.dumps(verdict) + "\n", encoding="utf-8")
    fd = _events_fd(run_dir)

    try:
        assert repair(run_dir, fd, 3.0) == ["trial-a"]
        assert repair(run_dir, fd, 4.0) == []
    finally:
        os.close(fd)

    assert read_rows(run_dir / "run-events.jsonl").rows == [
        verdict,
        {
            "arm": "arm-a",
            "reason": "harness_setup_failed",
            "t": 3.0,
            "task": "task-a",
            "trial": "trial-a",
            "type": "set_aside",
        },
    ]
    assert earlier_reasons(read_rows(run_dir / "run-events.jsonl").rows, "task-a", "arm-a") == (
        "harness_setup_failed",
    )


def test_repair_refuses_a_set_aside_trial_without_an_invalid_verdict(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    _write_trial(run_dir / "set-aside-trials" / "trial-a")
    fd = _events_fd(run_dir)

    try:
        with pytest.raises(ValueError, match="trial-a has no invalid trial_verdict"):
            repair(run_dir, fd, 3.0)
    finally:
        os.close(fd)


def test_set_aside_both_source_and_destination_present_raises(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    _write_trial(run_dir / "harbor-job" / "trial-a")
    _write_trial(run_dir / "set-aside-trials" / "trial-a")
    fd = _events_fd(run_dir)

    try:
        with pytest.raises(FileExistsError):
            set_aside(run_dir, "trial-a", "task-a", "arm-a", "invalid", fd, 1.0)
    finally:
        os.close(fd)


def test_set_aside_counts_and_earlier_reasons_use_task_and_arm() -> None:
    events = [
        {"type": "set_aside", "task": "task-a", "arm": "arm-a", "reason": "first"},
        {"type": "set_aside", "task": "task-a", "arm": "arm-a", "reason": "second"},
        {"type": "set_aside", "task": "task-a", "arm": "arm-b", "reason": "other"},
        {"type": "trial_verdict", "task": "task-a", "arm": "arm-a", "reason": "ignored"},
    ]

    assert set_aside_counts(events) == {("task-a", "arm-a"): 2, ("task-a", "arm-b"): 1}
    assert earlier_reasons(events, "task-a", "arm-a") == ("first", "second")
    assert earlier_reasons(events, "task-b", "arm-a") == ()
