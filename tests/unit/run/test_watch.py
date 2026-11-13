from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from harbor_research_runner.records.limits import Limits
from harbor_research_runner.run.watch import (
    build_snapshot,
    judge_all_remaining,
    judge_finished,
)


def _session(run_dir: Path):
    return SimpleNamespace(
        run_dir=run_dir,
        limits=Limits(max_set_asides_per_slot=2, verifier_timeout_multiplier=1.0),
        event=lambda event: _append_event(run_dir, event),
        oom_watch_alive=lambda: True,
        memory_watch_alive=lambda: True,
    )


def _append_event(run_dir: Path, event) -> None:
    path = run_dir / "run-events.jsonl"
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(event.to_row(), sort_keys=True) + "\n")


def _trial(job_dir: Path, name: str, *, result: bool = True, exception: str | None = None):
    trial_dir = job_dir / name
    trial_dir.mkdir(parents=True)
    (trial_dir / "config.json").write_text(
        json.dumps(
            {
                "task": {
                    "name": "terminal-bench/task-a",
                    "ref": "sha256:" + "a" * 64,
                    "source": "terminal-bench/terminal-bench-2-1",
                },
                "trial_name": name,
                "agent": {"kwargs": {"arm_label": "arm-a"}},
            }
        ),
        encoding="utf-8",
    )
    if result:
        (trial_dir / "result.json").write_text(
            json.dumps(
                {
                    "task_name": "terminal-bench/task-a",
                    "trial_name": name,
                    "verifier_result": {"rewards": {"reward": 1.0}},
                    "exception_info": (
                        {"exception_type": exception, "occurred_at": "2026-11-12T00:00:01.500000Z"}
                        if exception
                        else None
                    ),
                    "environment_setup": {
                        "started_at": "2026-11-12T00:00:00Z",
                        "finished_at": "2026-11-12T00:00:01Z",
                    },
                    "agent_setup": {
                        "started_at": "2026-11-12T00:00:01Z",
                        "finished_at": "2026-11-12T00:00:02Z",
                    },
                    "agent_execution": {
                        "started_at": "2026-11-12T00:00:02Z",
                        "finished_at": "2026-11-12T00:00:03Z",
                    },
                    "verifier": {
                        "started_at": "2026-11-12T00:00:03Z",
                        "finished_at": "2026-11-12T00:00:05Z",
                    },
                }
            ),
            encoding="utf-8",
        )
    return trial_dir


def _events(run_dir: Path):
    return [json.loads(line) for line in (run_dir / "run-events.jsonl").read_text().splitlines()]


def test_finished_clean_trial_writes_one_valid_verdict(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    job_dir = run_dir / "harbor-job"
    job_dir.mkdir()
    _trial(job_dir, "trial-a")
    (run_dir / "proxy-ledger.jsonl").write_text("", encoding="utf-8")
    (run_dir / "resource-stats.jsonl").write_text("", encoding="utf-8")
    judged: set[str] = set()

    assert judge_finished(_session(run_dir), job_dir, judged, 10.0) == ["trial-a"]
    assert _events(run_dir)[0]["valid"] is True


def test_finished_trial_is_judged_once(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    job_dir = run_dir / "harbor-job"
    job_dir.mkdir()
    _trial(job_dir, "trial-a")
    (run_dir / "proxy-ledger.jsonl").write_text("", encoding="utf-8")
    (run_dir / "resource-stats.jsonl").write_text("", encoding="utf-8")
    judged: set[str] = set()
    session = _session(run_dir)

    judge_finished(session, job_dir, judged, 10.0)
    assert judge_finished(session, job_dir, judged, 11.0) == []
    assert len(_events(run_dir)) == 1


def test_resultless_trial_is_judged_as_never_finished(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    job_dir = run_dir / "harbor-job"
    job_dir.mkdir()
    _trial(job_dir, "trial-a", result=False)
    (run_dir / "proxy-ledger.jsonl").write_text("", encoding="utf-8")
    (run_dir / "resource-stats.jsonl").write_text("", encoding="utf-8")

    judge_all_remaining(_session(run_dir), job_dir, set(), 10.0)
    assert _events(run_dir)[0]["reason"] == "never_finished"


def test_rebuilt_judged_set_prevents_rejudging(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    job_dir = run_dir / "harbor-job"
    job_dir.mkdir()
    _trial(job_dir, "trial-a")
    (run_dir / "proxy-ledger.jsonl").write_text("", encoding="utf-8")
    (run_dir / "resource-stats.jsonl").write_text("", encoding="utf-8")
    _append_event(
        run_dir, SimpleNamespace(to_row=lambda: {"type": "trial_verdict", "trial": "trial-a"})
    )

    assert judge_finished(_session(run_dir), job_dir, {"trial-a"}, 10.0) == []
    assert len(_events(run_dir)) == 1


def test_open_trial_remains_in_flight_until_verdict_after_close(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    job_dir = run_dir / "harbor-job"
    job_dir.mkdir()
    machine_check = run_dir / "machine-check.json"
    _write_machine_check(
        machine_check, [{"name": "task-a", "agent_budget_s": 60, "verifier_budget_s": 10}]
    )
    (run_dir / "proxy-ledger.jsonl").write_text(
        json.dumps({"type": "open", "seq": 1, "t": 90, "trial": "trial-a"})
        + "\n"
        + json.dumps({"type": "close", "seq": 2, "t": 95, "trial": "trial-a"})
        + "\n",
        encoding="utf-8",
    )
    (run_dir / "resource-stats.jsonl").write_text("", encoding="utf-8")
    session = SimpleNamespace(
        oom_watch_alive=lambda: True,
        memory_watch_alive=lambda: True,
        run_dir=run_dir,
        job=SimpleNamespace(machine_check=machine_check),
        metadata=SimpleNamespace(task_set={"tasks": [{"name": "task-a"}]}),
    )

    snapshot = build_snapshot(session, job_dir, 1, 101, True, 100)

    assert snapshot.open_trials == 0
    assert snapshot.in_flight_trials == 1

    _append_event(
        run_dir,
        SimpleNamespace(to_row=lambda: {"type": "trial_verdict", "trial": "trial-a"}),
    )
    judged_snapshot = build_snapshot(session, job_dir, 1, 102, True, 100)

    assert judged_snapshot.in_flight_trials == 0


def test_repeated_agent_setup_failure_is_valid_after_set_aside(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    job_dir = run_dir / "harbor-job"
    job_dir.mkdir()
    _trial(job_dir, "trial-a", exception="HarnessSetupError")
    (run_dir / "proxy-ledger.jsonl").write_text("", encoding="utf-8")
    (run_dir / "resource-stats.jsonl").write_text("", encoding="utf-8")
    _append_event(
        run_dir,
        SimpleNamespace(
            to_row=lambda: {
                "type": "set_aside",
                "trial": "trial-old",
                "task": "terminal-bench/task-a",
                "arm": "arm-a",
                "reason": "harness_setup_failed",
                "t": 1.0,
            }
        ),
    )

    judge_finished(_session(run_dir), job_dir, set(), 10.0)
    verdict = _events(run_dir)[1]
    assert verdict["valid"] is True
    assert "harness_could_not_start" in verdict["labels"]


def _write_machine_check(path: Path, tasks: list[dict]) -> None:
    path.write_text(
        json.dumps(
            {
                "created_t": 1.0,
                "tool_version": "0.1.0",
                "harbor_version": "0.23.0",
                "host_fingerprint": "host",
                "task_source": {},
                "concurrency": 1,
                "tasks": tasks,
                "included": [row["name"] for row in tasks],
                "verifier_timeout_multiplier": 1.0,
            }
        ),
        encoding="utf-8",
    )


def _snapshot_session(run_dir: Path, selected_tasks: list[str]):
    machine_check_path = run_dir / "machine-check.json"
    return SimpleNamespace(
        run_dir=run_dir,
        job=SimpleNamespace(machine_check=machine_check_path),
        metadata=SimpleNamespace(
            task_set={"tasks": [{"name": name, "digest": "sha256:task"} for name in selected_tasks]}
        ),
        limits=Limits(max_set_asides_per_slot=2, verifier_timeout_multiplier=1.0),
        oom_watch_alive=lambda: True,
        memory_watch_alive=lambda: True,
    )


def test_snapshot_uses_largest_selected_agent_and_verifier_budgets(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_machine_check(
        run_dir / "machine-check.json",
        [
            {"name": "bn-fit", "agent_budget_s": 3600, "verifier_budget_s": 3600},
            {"name": "git-multibranch", "agent_budget_s": 900, "verifier_budget_s": 900},
            {"name": "unselected", "agent_budget_s": 7200, "verifier_budget_s": 7200},
        ],
    )
    session = _snapshot_session(run_dir, ["bn-fit", "git-multibranch"])
    job_dir = run_dir / "harbor-job"
    job_dir.mkdir()

    snapshot = build_snapshot(session, job_dir, 100.0, 101.0, True)

    assert snapshot.longest_agent_budget_s == 3600
    assert snapshot.longest_verifier_budget_s == 3600


def test_snapshot_requires_agent_budget_for_each_selected_task(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_machine_check(
        run_dir / "machine-check.json",
        [{"name": "bn-fit", "verifier_budget_s": 3600}],
    )
    session = _snapshot_session(run_dir, ["bn-fit"])
    job_dir = run_dir / "harbor-job"
    job_dir.mkdir()

    try:
        build_snapshot(session, job_dir, 100.0, 101.0, True)
    except ValueError as exc:
        assert "bn-fit" in str(exc)
    else:
        raise AssertionError("missing agent_budget_s should be rejected")
