from __future__ import annotations

import hashlib
import json
import stat
import subprocess
import time
from pathlib import Path

import pytest
import yaml

from harbor_research_runner.docker_host.processes import pids_naming_run
from harbor_research_runner.records.jsonl import read_rows
from harbor_research_runner.run.process_group import HARBOR_STOP_GRACE_S

pytestmark = pytest.mark.timeout(300)


def _resume_after_quota(
    e2e_run,
) -> tuple[subprocess.CompletedProcess[str], subprocess.CompletedProcess[str]]:
    halted = e2e_run.run()
    assert halted.returncode == 2, halted.stdout
    e2e_run.upstream.script[:] = ["ok"]
    resumed = e2e_run.resume()
    assert resumed.returncode == 0, resumed.stdout
    return halted, resumed


def _trial_locations(run_dir: Path, trial: str) -> list[Path]:
    return [
        root / trial
        for root in (
            run_dir / "canary-job",
            run_dir / "harbor-job",
            run_dir / "set-aside-trials",
        )
        if (root / trial).is_dir()
    ]


def _ledger_trial_names(rows: list[dict]) -> set[str]:
    return {row["trial"] for row in rows if isinstance(row.get("trial"), str)}


def test_happy_path_two_arms_exits_zero(e2e_run) -> None:
    e2e_run.job_data["arms"] = [
        {**arm, "label": f"arm-{index + 1}"}
        for index, arm in enumerate(e2e_run.job_data["arms"] * 2)
    ]
    e2e_run.job_data["repetitions"] = 2
    e2e_run.job_path.write_text(yaml.safe_dump(e2e_run.job_data, sort_keys=True), encoding="utf-8")
    result = e2e_run.run()

    assert result.returncode == 0, result.stdout
    verdicts = [
        row
        for row in e2e_run.events
        if row.get("type") == "trial_verdict"
        and row.get("task") == "say-hello"
        and (e2e_run.run_dir / "harbor-job" / row["trial"]).is_dir()
    ]
    assert len(verdicts) == 4
    assert all(row["valid"] for row in verdicts)
    opens = [row["trial"] for row in e2e_run.ledger if row.get("type") == "open"]
    closes = [row["trial"] for row in e2e_run.ledger if row.get("type") == "close"]
    assert sorted(opens) == sorted(closes)
    seqs = [row["seq"] for row in e2e_run.ledger]
    assert seqs == list(range(1, len(seqs) + 1))
    for path in (e2e_run.run_dir, *e2e_run.run_dir.rglob("*")):
        assert path.stat().st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH) == 0


@pytest.mark.parametrize(
    "e2e_run",
    [{"repetitions": 2, "upstream_script": ["ok", "ok", "quota"]}],
    indirect=True,
)
def test_halt_then_resume_keeps_every_cancelled_trial_in_set_aside(e2e_run) -> None:
    _resume_after_quota(e2e_run)

    invalid_trials = {
        row["trial"]
        for row in e2e_run.events
        if row.get("type") == "trial_verdict" and row.get("valid") is False
    }
    assert invalid_trials
    assert all((e2e_run.run_dir / "set-aside-trials" / trial).is_dir() for trial in invalid_trials)
    trial_names = _ledger_trial_names(e2e_run.ledger)
    assert trial_names
    assert all(len(_trial_locations(e2e_run.run_dir, trial)) == 1 for trial in trial_names)
    canary_trials = {
        path.name for path in (e2e_run.run_dir / "canary-job").iterdir() if path.is_dir()
    }
    non_canary = trial_names - canary_trials
    assert all(
        (e2e_run.run_dir / "harbor-job" / trial).is_dir()
        or (e2e_run.run_dir / "set-aside-trials" / trial).is_dir()
        for trial in non_canary
    )


@pytest.mark.parametrize(
    "e2e_run",
    [{"repetitions": 2, "fake": {"sleep_s": 10}}],
    indirect=True,
)
def test_killed_runner_resume_sets_aside_unfinished_trial_and_reruns_slot(e2e_run) -> None:
    e2e_run.start()
    unfinished = e2e_run.wait_for_main_trial()
    e2e_run.kill_runner()

    resumed = e2e_run.resume()

    assert resumed.returncode == 0, resumed.stdout
    assert (e2e_run.run_dir / "set-aside-trials" / unfinished).is_dir()
    events = e2e_run.events
    assert any(
        row.get("type") == "set_aside"
        and row.get("trial") == unfinished
        and row.get("reason") == "never_finished"
        for row in events
    )
    valid = [
        row
        for row in events
        if row.get("type") == "trial_verdict"
        and row.get("task") == "say-hello"
        and row.get("arm") == "arm-1"
        and row.get("valid") is True
        and (e2e_run.run_dir / "harbor-job" / row["trial"]).is_dir()
    ]
    assert len(valid) == 2


@pytest.mark.parametrize(
    "e2e_run",
    [{"repetitions": 1, "fake": {"sleep_s": 75, "continue_on_error": True}}],
    indirect=True,
)
@pytest.mark.timeout(600)
def test_runner_only_death_stops_harbor_before_cleanup_and_resume(e2e_run) -> None:
    e2e_run.start()
    e2e_run.wait_for_main_trial(timeout=150)
    stopped_after = e2e_run.kill_runner_only(stop_within_s=HARBOR_STOP_GRACE_S + 20)
    assert stopped_after < HARBOR_STOP_GRACE_S + 20
    assert not pids_naming_run(e2e_run.run_dir)

    cleaned = e2e_run.clean_docker(apply=True)
    assert cleaned.returncode == 0, cleaned.stdout + cleaned.stderr
    e2e_run.start(resume=True)
    resumed = e2e_run.wait()
    assert resumed.returncode == 0, resumed.stdout

    evidence_rows = read_rows(e2e_run.evidence_dir / e2e_run.run_id / "trials.jsonl").rows
    metadata = json.loads((e2e_run.run_dir / "run-metadata.json").read_text(encoding="utf-8"))
    final_rows = [row for row in evidence_rows if row["is_final"]]
    for task in metadata["task_set"]["tasks"]:
        for arm in metadata["arms"]:
            slot_trials = [
                row
                for row in final_rows
                if row["task"] == task["name"] and row["arm"] == arm["label"]
            ]
            assert len(slot_trials) == metadata["job"]["repetitions"]
            assert all(row["valid"] and row["measured"] for row in slot_trials)
    trial_ids = [row["trial"] for row in evidence_rows]
    assert len(trial_ids) == len(set(trial_ids))


@pytest.mark.parametrize(
    "e2e_run",
    [{"repetitions": 2, "upstream_script": ["ok", "ok", "quota"]}],
    indirect=True,
)
def test_every_ledger_trial_is_accounted_for_after_reruns(e2e_run) -> None:
    _resume_after_quota(e2e_run)

    evidence_rows = read_rows(e2e_run.evidence_dir / e2e_run.run_id / "trials.jsonl").rows
    evidence_trials = {row["trial"] for row in evidence_rows}
    assert _ledger_trial_names(e2e_run.ledger) <= evidence_trials


@pytest.mark.parametrize(
    "e2e_run",
    [{"repetitions": 2, "upstream_script": ["ok", "ok", "quota"]}],
    indirect=True,
)
def test_lifecycle_repairs_interrupted_set_aside(e2e_run) -> None:
    halted = e2e_run.run()
    assert halted.returncode == 2, halted.stdout
    verdict = next(
        row
        for row in e2e_run.events
        if row.get("type") == "trial_verdict" and row.get("valid") is False
    )
    invalid = verdict["trial"]
    original = e2e_run.run_dir / "harbor-job" / invalid
    destination = e2e_run.run_dir / "set-aside-trials" / invalid
    destination.parent.mkdir(parents=True, exist_ok=True)
    original.rename(destination)
    e2e_run.upstream.script[:] = ["ok"]

    resumed = e2e_run.resume()

    assert resumed.returncode == 0, resumed.stdout
    repaired = [
        row
        for row in e2e_run.events
        if row.get("type") == "set_aside" and row.get("trial") == invalid
    ]
    assert len(repaired) == 1
    assert repaired[0]["reason"] == verdict["reason"]


@pytest.mark.parametrize(
    "e2e_run",
    [{"repetitions": 2, "upstream_script": ["ok", "ok", "quota"]}],
    indirect=True,
)
def test_resume_never_rewrites_run_metadata(e2e_run) -> None:
    halted = e2e_run.run()
    assert halted.returncode == 2, halted.stdout
    metadata = e2e_run.run_dir / "run-metadata.json"
    before = hashlib.sha256(metadata.read_bytes()).hexdigest()
    e2e_run.upstream.script[:] = ["ok"]

    resumed = e2e_run.resume()

    assert resumed.returncode == 0, resumed.stdout
    after = hashlib.sha256(metadata.read_bytes()).hexdigest()
    assert after == before
    assert any(row.get("type") == "resume" for row in e2e_run.events)


@pytest.mark.parametrize(
    "harbor_job",
    [
        {
            "defer_run": True,
            "fake": {
                "sleep_s": 180,
                "detached_heartbeat": "/app/lifecycle-heartbeat.txt",
            },
        }
    ],
    indirect=True,
)
def test_stop_script_kills_harness_tree_but_not_detached_service(harbor_job) -> None:
    harbor_job.start()
    deadline = time.monotonic() + 150
    container_id = None
    while time.monotonic() < deadline:
        opened = any(row.get("type") == "open" for row in read_rows(harbor_job.ledger_path).rows)
        if not opened:
            if harbor_job.process is not None and harbor_job.process.poll() is not None:
                output, _ = harbor_job.process.communicate()
                raise AssertionError(f"Harbor exited before opening a trial: {output}")
            time.sleep(0.5)
            continue
        result = subprocess.run(
            ["docker", "ps", "--format", "{{.ID}} {{.Names}}"],
            capture_output=True,
            text=True,
            check=True,
        )
        matches = [line.split()[0] for line in result.stdout.splitlines() if "__env-main-" in line]
        if matches:
            container_id = matches[-1]
            top = subprocess.run(
                ["docker", "top", container_id], capture_output=True, text=True, check=True
            )
            heartbeat = subprocess.run(
                ["docker", "exec", container_id, "cat", "/app/lifecycle-heartbeat.txt"],
                capture_output=True,
                text=True,
                check=False,
            )
            if (
                heartbeat.returncode == 0
                and "fake_script.py" not in top.stdout
                and len(heartbeat.stdout.splitlines()) > 1
            ):
                break
        time.sleep(0.5)
    assert container_id is not None
    assert "fake_script.py" not in top.stdout
    assert len(heartbeat.stdout.splitlines()) > 1
    harbor_job.wait(timeout=60)


@pytest.mark.parametrize("e2e_run", [{"write_machine_check": False}], indirect=True)
def test_machine_check_output_feeds_a_run_that_builds_evidence(e2e_run) -> None:
    checked = e2e_run.check_machine()
    assert checked.returncode == 0, checked.stdout + checked.stderr
    assert e2e_run.machine_check_path.is_file()

    result = e2e_run.run()

    assert result.returncode == 0, result.stdout
    evidence = e2e_run.evidence_dir / e2e_run.run_id
    assert (evidence / "manifest.json").is_file()
    report = (evidence / "report.md").read_text(encoding="utf-8")
    assert "say-hello" in report
    assert "0 excluded by the machine check (none)" in report


@pytest.mark.parametrize("e2e_run", [{"fake": {"sleep_s": 10}}], indirect=True)
def test_clean_docker_removes_residue_after_halt(e2e_run) -> None:
    e2e_run.start()
    e2e_run.wait_for_main_trial()
    e2e_run.kill_runner()
    e2e_run.kill_proxy()

    listed = e2e_run.clean_docker(apply=False)
    assert listed.returncode == 1
    assert "__env-main-" in listed.stdout + listed.stderr
    applied = e2e_run.clean_docker(apply=True)
    assert applied.returncode == 0, applied.stdout + applied.stderr
    assert e2e_run.run().returncode == 0


@pytest.mark.parametrize(
    "e2e_run",
    [
        {
            "upstream_script": ["ok", "http_502"],
            "limits": {"max_set_asides_per_slot": 1},
        }
    ],
    indirect=True,
)
def test_slot_at_set_aside_cap_finishes_with_exit_three(e2e_run) -> None:
    result = e2e_run.run()

    assert result.returncode == 3, result.stdout
    finish = next(row for row in e2e_run.events if row.get("type") == "finish")
    assert finish["slots_incomplete"] == 1


@pytest.mark.parametrize("e2e_run", [{"fake": {"sleep_s": 10}}], indirect=True)
def test_second_run_on_the_same_host_is_refused_while_first_is_live(e2e_run) -> None:
    e2e_run.start()
    e2e_run.wait_for_run()
    second_job, second_runs_dir = e2e_run.second_job()
    refused = e2e_run.cli(
        [
            "run",
            str(second_job),
            "--runs-dir",
            str(second_runs_dir),
            "--evidence-dir",
            str(e2e_run.evidence_dir),
            "--manifest",
            str(e2e_run.manifest_path),
        ]
    )

    assert refused.returncode == 1
    assert "single_run" in refused.stdout + refused.stderr
    check_events = [
        row
        for path in second_runs_dir.rglob("run-events.jsonl")
        for row in read_rows(path).rows
        if row.get("type") == "launch_check" and row.get("name") == "single_run"
    ]
    assert check_events
    assert all(row["ok"] is False for row in check_events)
    assert not any(second_runs_dir.rglob("proxy-ledger.jsonl"))
    assert e2e_run.process is not None and e2e_run.process.poll() is None


@pytest.mark.parametrize("e2e_run", [{"fake": {"sleep_s": 10}}], indirect=True)
def test_clean_docker_apply_refuses_while_another_runs_directory_is_live(e2e_run) -> None:
    e2e_run.start()
    e2e_run.wait_for_run()
    second_runs_dir = e2e_run.root / "cleaner-runs"
    second_runs_dir.mkdir()
    before = subprocess.run(
        ["docker", "ps", "-a", "--format", "{{.Names}}"],
        capture_output=True,
        text=True,
        check=True,
    )

    refused = e2e_run.cli(["clean-docker", "--apply", "--runs-dir", str(second_runs_dir)])

    after = subprocess.run(
        ["docker", "ps", "-a", "--format", "{{.Names}}"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert refused.returncode == 1
    assert e2e_run.process is not None and e2e_run.process.poll() is None
    assert sorted(before.stdout.splitlines()) == sorted(after.stdout.splitlines())
