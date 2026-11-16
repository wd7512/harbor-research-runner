from __future__ import annotations

import time
from pathlib import Path

import pytest
import yaml

from harbor_research_runner.job_config.job_file import load_job
from harbor_research_runner.records.jsonl import read_rows
from tests.support.fake_upstream import RawResponse

pytestmark = pytest.mark.timeout(600)


def _write_job(e2e_run) -> None:
    e2e_run.job_path.write_text(yaml.safe_dump(e2e_run.job_data, sort_keys=True), encoding="utf-8")
    load_job(e2e_run.job_path)


def _main_trial_dirs(run_dir: Path) -> list[Path]:
    return [
        trial
        for folder in (run_dir / "harbor-job", run_dir / "set-aside-trials")
        if folder.is_dir()
        for trial in folder.iterdir()
        if trial.is_dir()
    ]


def _run_events(e2e_run) -> tuple[Path, list[dict]]:
    run_dirs = [path for path in e2e_run.runs_dir.iterdir() if path.is_dir()]
    assert len(run_dirs) == 1
    run_dir = run_dirs[0]
    return run_dir, read_rows(run_dir / "run-events.jsonl").rows


@pytest.mark.parametrize(
    "e2e_run",
    [{"agent_timeout_s": 10, "fake": {"sleep_s": 20, "detached_requesting_s": 20}}],
    indirect=True,
)
def test_requests_after_agent_timeout_are_refused_and_recorded(e2e_run) -> None:
    result = e2e_run.run()

    assert result.returncode == 0, result.stdout
    main_trials = {path.name for path in _main_trial_dirs(e2e_run.run_dir)}
    trial = next(
        row["trial"]
        for row in e2e_run.ledger
        if row.get("type") == "open" and row["trial"] in main_trials
    )
    rows = [row for row in e2e_run.ledger if row.get("trial") == trial]
    close = next((row for row in rows if row.get("type") == "close"), None)
    assert close is not None, f"trial {trial} has no close row"
    assert any(
        row.get("type") == "refused" and row.get("reason") == "closed" and row["seq"] > close["seq"]
        for row in rows
    )
    assert not any(row.get("type") == "request_start" and row["seq"] > close["seq"] for row in rows)
    verdict = next(
        row
        for row in e2e_run.events
        if row.get("type") == "trial_verdict" and row.get("trial") == trial
    )
    assert verdict["valid"] is True
    assert {"timed_out", "refused_after_close"} <= set(verdict["labels"])


@pytest.mark.parametrize(
    "e2e_run",
    [
        {
            "upstream_script": ["ok", "ok", "ok", "ok", "ok", "http_502", "ok", "ok"],
            "fake": {"requests": 4, "continue_on_error": True},
        }
    ],
    indirect=True,
)
def test_one_tolerated_upstream_error_keeps_trial_valid(e2e_run) -> None:
    result = e2e_run.run()

    assert result.returncode == 0, result.stdout
    trials = _main_trial_dirs(e2e_run.run_dir)
    assert len(trials) == 1
    request_ends = [
        row
        for row in e2e_run.ledger
        if row.get("type") == "request_end" and row.get("trial") == trials[0].name
    ]
    assert [row["upstream_status"] for row in request_ends] == [
        "ok",
        "server_error",
        "ok",
        "ok",
    ]
    verdict = next(
        row
        for row in e2e_run.events
        if row.get("type") == "trial_verdict" and row.get("trial") == trials[0].name
    )
    assert verdict["valid"] is True
    assert not any(row.get("type") == "set_aside" for row in e2e_run.events)


@pytest.mark.parametrize(
    "e2e_run",
    [
        {
            "upstream_script": ["ok", "ok", "http_502", "ok", "ok"],
            "fake": {"requests": 2},
        }
    ],
    indirect=True,
)
def test_upstream_terminal_error_sets_trial_aside_and_reruns(e2e_run) -> None:
    result = e2e_run.run()

    assert result.returncode == 0, result.stdout
    set_asides = [
        row
        for row in e2e_run.events
        if row.get("type") == "set_aside" and row.get("reason") == "upstream_terminal_error"
    ]
    assert len(set_asides) == 1
    assert len(_main_trial_dirs(e2e_run.run_dir)) == 2
    assert sum(row.get("type") == "open" for row in e2e_run.ledger) == 3


@pytest.mark.parametrize(
    "e2e_run",
    [{"upstream_script": ["ok", "quota"]}],
    indirect=True,
)
def test_quota_error_halts_e2e_run(e2e_run) -> None:
    result = e2e_run.run()

    assert result.returncode == 2, result.stdout
    assert any(
        row.get("type") == "halt" and row.get("reason") == "quota_exhausted"
        for row in e2e_run.events
    )
    assert len(_main_trial_dirs(e2e_run.run_dir)) == 1
    assert sum(row.get("type") == "open" for row in e2e_run.ledger) == 2


def test_dead_proxy_halts_run(e2e_run) -> None:
    e2e_run.upstream.script[:] = ["ok", RawResponse(chunks=[], hold_open=True)]
    e2e_run.start()
    e2e_run.wait_for_main_trial()
    deadline = time.monotonic() + 30
    while len(e2e_run.upstream.requests) < 2 and time.monotonic() < deadline:
        if e2e_run.process is not None and e2e_run.process.poll() is not None:
            output, _ = e2e_run.process.communicate()
            raise AssertionError(f"runner exited before the held upstream request: {output}")
        time.sleep(0.05)
    assert len(e2e_run.upstream.requests) >= 2

    e2e_run.kill_proxy()
    halted = e2e_run.wait(timeout=60)

    assert halted.returncode == 2, halted.stdout
    assert any(
        row.get("type") == "halt" and row.get("reason") == "proxy_dead" for row in e2e_run.events
    )
    e2e_run.upstream.script[:] = ["ok"]
    resumed = e2e_run.resume()

    assert resumed.returncode == 0, resumed.stdout


@pytest.mark.parametrize("e2e_run", [{"upstream_script": ["http_502"]}], indirect=True)
def test_canary_failure_refuses_launch(e2e_run) -> None:
    result = e2e_run.run()

    assert result.returncode == 1, result.stdout
    assert not (e2e_run.run_dir / "harbor-job").exists()
    assert any(
        row.get("type") == "harbor_exit" and row.get("job") == "canary" for row in e2e_run.events
    )
    assert any(
        row.get("type") == "trial_verdict" and row.get("valid") is False for row in e2e_run.events
    )


def _use_unlisted_opencode_arm(e2e_run) -> None:
    arm = dict(e2e_run.job_data["arms"][0])
    arm.pop("fake", None)
    arm["harness"] = "opencode"
    arm["commit"] = "f" * 40
    e2e_run.job_data["arms"] = [arm]
    _write_job(e2e_run)


def test_unlisted_image_refuses_launch(e2e_run) -> None:
    _use_unlisted_opencode_arm(e2e_run)

    result = e2e_run.run()

    run_dir, events = _run_events(e2e_run)
    assert result.returncode == 1, result.stdout
    assert not (run_dir / "proxy-ledger.jsonl").exists()
    assert any(
        row.get("type") == "launch_check"
        and row.get("name") == "arms_pinned"
        and row["ok"] is False
        for row in events
    )


def test_e2e_job_naming_task_outside_machine_check_is_refused(e2e_run) -> None:
    e2e_run.job_data["tasks"]["include"] = ["not-in-machine-check"]
    _write_job(e2e_run)

    result = e2e_run.run()

    _run_dir, events = _run_events(e2e_run)
    assert result.returncode == 1, result.stdout
    assert any(
        row.get("type") == "launch_check"
        and row.get("name") == "tasks_included"
        and row["ok"] is False
        for row in events
    )


def test_all_checks_are_reported_together(e2e_run) -> None:
    _use_unlisted_opencode_arm(e2e_run)
    e2e_run.job_data["tasks"]["include"] = ["not-in-machine-check"]
    _write_job(e2e_run)

    result = e2e_run.run()

    _run_dir, events = _run_events(e2e_run)
    assert result.returncode == 1, result.stdout
    failed = {
        row["name"] for row in events if row.get("type") == "launch_check" and row["ok"] is False
    }
    assert {"arms_pinned", "tasks_included"} <= failed
