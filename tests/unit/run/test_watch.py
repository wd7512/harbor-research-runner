from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from harbor_research_runner.records.limits import Limits
from harbor_research_runner.run.watch import (
    build_snapshot,
    judge_all_remaining,
    judge_finished,
)
from harbor_research_runner.trial_validity.run_health import should_halt


def _session(run_dir: Path):
    resource_path = run_dir / "resource-stats.jsonl"
    if resource_path.exists() and not resource_path.read_bytes():
        phase_start = datetime(2026, 11, 12, 0, 0, 2, tzinfo=UTC).timestamp()
        fields = {
            "type": "sample",
            "mem_available_mb": 8192,
            "swapped_out_pages": 0,
            "disk_free_gb": 100,
            "load1": 0.5,
            "trial_containers": 1,
            "docker_ok": True,
        }
        resource_path.write_text(
            "".join(
                json.dumps({**fields, "t": timestamp}, sort_keys=True) + "\n"
                for timestamp in (phase_start - 1, phase_start + 4)
            ),
            encoding="utf-8",
        )
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


def _write_inflight_ledger(run_dir: Path, *, closed: bool = False) -> None:
    rows = [
        {
            "type": "open",
            "seq": 1,
            "t": 1.0,
            "trial": "trial-a",
            "task": "terminal-bench/task-a",
            "arm": "arm-a",
        },
        {
            "type": "request_start",
            "seq": 2,
            "t": 2.0,
            "trial": "trial-a",
            "request_id": "request-a",
            "model": "model-a",
            "is_stream": True,
            "prompt_chars": 1,
            "paced_wait_s": 0.0,
        },
    ]
    if closed:
        rows.append({"type": "close", "seq": 3, "t": 3.0, "trial": "trial-a"})
    (run_dir / "proxy-ledger.jsonl").write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows), encoding="utf-8"
    )


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


def test_missing_memory_telemetry_marks_trial_unmeasured(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    job_dir = run_dir / "harbor-job"
    job_dir.mkdir()
    _trial(job_dir, "trial-a")
    (run_dir / "proxy-ledger.jsonl").write_text("", encoding="utf-8")
    (run_dir / "resource-stats.jsonl").write_text("", encoding="utf-8")
    (run_dir / "docker-oom-events.jsonl").write_text("", encoding="utf-8")
    _append_event(run_dir, SimpleNamespace(to_row=lambda: {"type": "harbor_start", "t": 1}))

    judge_finished(_session(run_dir), job_dir, set(), 10.0)

    event = _events(run_dir)[-1]
    assert event["valid"] is True
    assert event["measured"] is False
    assert event["unmeasured_reason"] == "telemetry_missing"


def test_malformed_host_sample_reaches_trial_verdict(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    job_dir = run_dir / "harbor-job"
    job_dir.mkdir()
    trial_dir = _trial(job_dir, "trial-a")
    phase_start = datetime(2026, 11, 12, 0, 0, 2, tzinfo=UTC).timestamp()
    sample_fields = (
        '"mem_available_mb":8192,"swapped_out_pages":0,"disk_free_gb":100,'
        '"load1":0.5,"trial_containers":1,"docker_ok":true'
    )
    (run_dir / "resource-stats.jsonl").write_text(
        f'{{"type":"sample","t":{phase_start - 1},{sample_fields}}}\n'
        '{"type":"sample","t":\n'
        f'{{"type":"sample","t":{phase_start + 4},{sample_fields}}}\n',
        encoding="utf-8",
    )
    (run_dir / "proxy-ledger.jsonl").write_text(
        json.dumps(
            {
                "type": "open",
                "seq": 1,
                "t": phase_start,
                "trial": trial_dir.name,
                "task": "terminal-bench/task-a",
                "arm": "arm-a",
            }
        )
        + "\n"
        + json.dumps({"type": "close", "seq": 2, "t": phase_start + 3, "trial": trial_dir.name})
        + "\n",
        encoding="utf-8",
    )

    judge_finished(_session(run_dir), job_dir, set(), 10.0)

    event = json.loads((run_dir / "run-events.jsonl").read_text(encoding="utf-8"))
    assert event["measured"] is False
    assert event["unmeasured_reason"] == "host_samples_damaged"


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


def test_deadline_deferred_trial_has_deferred_verdict(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    job_dir = run_dir / "harbor-job"
    job_dir.mkdir()
    _trial(job_dir, "trial-a", result=False)
    (run_dir / "proxy-ledger.jsonl").write_text("", encoding="utf-8")
    (run_dir / "resource-stats.jsonl").write_text("", encoding="utf-8")
    _append_event(run_dir, SimpleNamespace(to_row=lambda: {"type": "halt", "reason": "deadline"}))

    judge_all_remaining(_session(run_dir), job_dir, set(), 10.0, halt_reason="deadline")

    event = _events(run_dir)[-1]
    assert event["valid"] is False
    assert event["reason"] == "deferred"
    assert event["instrument"] is None


def test_halt_invalidates_valid_inflight_trial(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    job_dir = run_dir / "harbor-job"
    job_dir.mkdir()
    trial_dir = _trial(job_dir, "trial-a", exception="NonZeroAgentExitCodeError")
    result = json.loads((trial_dir / "result.json").read_text(encoding="utf-8"))
    result["exception_info"]["occurred_at"] = "2026-11-12T00:00:02.500000Z"
    (trial_dir / "result.json").write_text(json.dumps(result), encoding="utf-8")
    _write_inflight_ledger(run_dir)
    (run_dir / "resource-stats.jsonl").write_text("", encoding="utf-8")

    judge_all_remaining(_session(run_dir), job_dir, set(), 10.0, halt_reason="proxy_dead")

    event = _events(run_dir)[0]
    assert event["valid"] is False
    assert event["reason"] == "run_halted"
    assert event["instrument"] is None


def test_halt_keeps_closed_trial_verdict(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    job_dir = run_dir / "harbor-job"
    job_dir.mkdir()
    trial_dir = _trial(job_dir, "trial-a", exception="NonZeroAgentExitCodeError")
    result = json.loads((trial_dir / "result.json").read_text(encoding="utf-8"))
    result["exception_info"]["occurred_at"] = "2026-11-12T00:00:02.500000Z"
    (trial_dir / "result.json").write_text(json.dumps(result), encoding="utf-8")
    _write_inflight_ledger(run_dir, closed=True)
    (run_dir / "resource-stats.jsonl").write_text("", encoding="utf-8")

    judge_all_remaining(_session(run_dir), job_dir, set(), 10.0, halt_reason="proxy_dead")

    event = _events(run_dir)[0]
    assert event["valid"] is True


def test_inflight_trial_without_halt_keeps_valid_verdict(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    job_dir = run_dir / "harbor-job"
    job_dir.mkdir()
    trial_dir = _trial(job_dir, "trial-a", exception="NonZeroAgentExitCodeError")
    result = json.loads((trial_dir / "result.json").read_text(encoding="utf-8"))
    result["exception_info"]["occurred_at"] = "2026-11-12T00:00:02.500000Z"
    (trial_dir / "result.json").write_text(json.dumps(result), encoding="utf-8")
    _write_inflight_ledger(run_dir)
    (run_dir / "resource-stats.jsonl").write_text("", encoding="utf-8")

    judge_all_remaining(_session(run_dir), job_dir, set(), 10.0)

    event = _events(run_dir)[0]
    assert event["valid"] is True


def test_deadline_trial_opened_at_proxy_keeps_never_finished_verdict(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    job_dir = run_dir / "harbor-job"
    job_dir.mkdir()
    _trial(job_dir, "trial-a", result=False)
    (run_dir / "proxy-ledger.jsonl").write_text(
        json.dumps({"type": "open", "trial": "trial-a"}) + "\n", encoding="utf-8"
    )
    (run_dir / "resource-stats.jsonl").write_text("", encoding="utf-8")

    judge_all_remaining(_session(run_dir), job_dir, set(), 10.0, halt_reason="deadline")

    event = _events(run_dir)[0]
    assert event["reason"] == "never_finished"
    assert event["instrument"] == "harbor"


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


def test_resume_resets_upstream_invalid_streak_window(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    job_dir = run_dir / "harbor-job"
    job_dir.mkdir()
    machine_check = run_dir / "machine-check.json"
    _write_machine_check(
        machine_check, [{"name": "task-a", "agent_budget_s": 60, "verifier_budget_s": 10}]
    )
    (run_dir / "proxy-ledger.jsonl").write_text("", encoding="utf-8")
    (run_dir / "resource-stats.jsonl").write_text(
        json.dumps(
            {
                "type": "sample",
                "t": 100,
                "mem_available_mb": 8192,
                "swapped_out_pages": 0,
                "disk_free_gb": 100,
                "load1": 0.5,
                "trial_containers": 0,
                "docker_ok": True,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    events = [
        {
            "type": "trial_verdict",
            "t": float(index),
            "trial": f"trial-{index}",
            "task": "task-a",
            "arm": "arm-a",
            "valid": False,
            "reason": "upstream_errors",
            "instrument": "proxy",
            "labels": [],
            "measured": True,
            "unmeasured_reason": None,
            "oom_source": None,
        }
        for index in range(5)
    ]
    events.extend(
        [
            {
                "type": "halt",
                "t": 5.0,
                "reason": "upstream_invalid_streak",
                "detail": "5 consecutive proxy-invalid trial verdicts",
            },
            {
                "type": "resume",
                "t": 6.0,
                "tool_version": "0.1.0",
                "tool_commit": "a" * 40,
                "pid": 123,
            },
        ]
    )
    (run_dir / "run-events.jsonl").write_text(
        "".join(json.dumps(event) + "\n" for event in events), encoding="utf-8"
    )
    session = SimpleNamespace(
        oom_watch_alive=lambda: True,
        memory_watch_alive=lambda: True,
        run_dir=run_dir,
        job=SimpleNamespace(machine_check=machine_check),
        metadata=SimpleNamespace(task_set={"tasks": [{"name": "task-a"}]}),
        limits=Limits(max_set_asides_per_slot=2, verifier_timeout_multiplier=1.0),
    )

    snapshot = build_snapshot(session, job_dir, 1, 101, True, 100)

    assert snapshot.verdicts == []
    assert should_halt(snapshot, session.limits) is None

    with (run_dir / "run-events.jsonl").open("a", encoding="utf-8") as stream:
        for index in range(5, 10):
            stream.write(
                json.dumps(
                    {
                        "type": "trial_verdict",
                        "t": float(index + 7),
                        "trial": f"trial-{index}",
                        "task": "task-a",
                        "arm": "arm-a",
                        "valid": False,
                        "reason": "upstream_errors",
                        "instrument": "proxy",
                        "labels": [],
                        "measured": True,
                        "unmeasured_reason": None,
                        "oom_source": None,
                    }
                )
                + "\n"
            )

    snapshot = build_snapshot(session, job_dir, 1, 102, True, 100)
    assert [row["trial"] for row in snapshot.verdicts] == [
        f"trial-{index}" for index in range(5, 10)
    ]
    assert should_halt(snapshot, session.limits)[0] == "upstream_invalid_streak"


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
