from __future__ import annotations

import stat
import subprocess
import time

import pytest
import yaml

from harbor_research_runner.records.jsonl import read_rows

pytestmark = pytest.mark.timeout(300)


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
