import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from harbor_research_runner.records.jsonl import read_rows
from harbor_research_runner.run.canary import canary_memory_mb
from harbor_research_runner.run.memory_watch import (
    kill_decision,
    run_memory_watch,
    sample_once,
    trial_memory_limits,
)

FIXTURES = Path(__file__).parents[2] / "fixtures" / "memory_watch"


def test_trial_memory_limit_lookup_keeps_null_limit(tmp_path: Path):
    (tmp_path / "machine-check.json").write_text(
        json.dumps(
            {
                "created_t": 1,
                "tool_version": "1",
                "harbor_version": "0.23.0",
                "host_fingerprint": "host",
                "task_source": {},
                "concurrency": 1,
                "tasks": [
                    {"name": "task-a", "memory_mb": 128},
                    {"name": "task-b", "memory_mb": None},
                ],
                "included": ["task-a", "task-b"],
                "verifier_timeout_multiplier": 1.0,
            }
        ),
        encoding="utf-8",
    )
    for trial, task in (("trial-a", "task-a"), ("trial-b", "task-b")):
        folder = tmp_path / "harbor-job" / trial
        folder.mkdir(parents=True)
        (folder / "config.json").write_text(
            json.dumps(
                {
                    "task": {"path": task},
                    "agent": {"kwargs": {"arm_label": "arm-a"}},
                }
            ),
            encoding="utf-8",
        )
    canary = tmp_path / "canary-job" / "canary__alpha"
    canary.mkdir(parents=True)
    (canary / "config.json").write_text(
        json.dumps(
            {
                "trial_name": "canary__alpha",
                "task": {"name": "canary/say-hello"},
                "agent": {"kwargs": {"arm_label": "arm-a"}},
            }
        ),
        encoding="utf-8",
    )

    assert trial_memory_limits(tmp_path) == {
        "trial-a": 128,
        "trial-b": None,
        "canary__alpha": canary_memory_mb(),
    }


def test_main_trial_missing_from_machine_check_raises(tmp_path: Path):
    (tmp_path / "machine-check.json").write_text(
        json.dumps(
            {
                "created_t": 1,
                "tool_version": "1",
                "harbor_version": "0.23.0",
                "host_fingerprint": "host",
                "task_source": {},
                "concurrency": 1,
                "tasks": [],
                "included": [],
                "verifier_timeout_multiplier": 1.0,
            }
        ),
        encoding="utf-8",
    )
    trial = tmp_path / "harbor-job" / "trial-a"
    trial.mkdir(parents=True)
    (trial / "config.json").write_text(
        json.dumps(
            {
                "trial_name": "trial-a",
                "task": {"path": "task-a"},
                "agent": {"kwargs": {"arm_label": "arm-a"}},
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="no memory limit"):
        trial_memory_limits(tmp_path)


def test_kill_decision_kills_once_and_only_above_limit():
    killed = set()

    assert kill_decision("container-a", 101, 100, killed)
    assert not kill_decision("container-a", 101, 100, killed)
    assert not kill_decision("container-b", 100, 100, killed)
    assert not kill_decision("container-c", 101, None, killed)


def test_sample_records_memory_and_kills_over_limit_container(tmp_path: Path):
    run_dir = tmp_path
    (run_dir / "machine-check.json").write_text(
        json.dumps(
            {
                "created_t": 1,
                "tool_version": "1",
                "harbor_version": "0.23.0",
                "host_fingerprint": "host",
                "task_source": {},
                "concurrency": 1,
                "tasks": [{"name": "task-a", "memory_mb": 3}],
                "included": ["task-a"],
                "verifier_timeout_multiplier": 1.0,
            }
        ),
        encoding="utf-8",
    )
    trial_dir = run_dir / "harbor-job" / "trial-a__abc"
    trial_dir.mkdir(parents=True)
    (trial_dir / "config.json").write_text(
        json.dumps(
            {
                "trial_name": "trial-a__abc",
                "task": {"path": "task-a"},
                "agent": {"kwargs": {"arm_label": "arm-a"}},
            }
        ),
        encoding="utf-8",
    )
    commands = []

    def run(command):
        commands.append(command)
        if "stats" in command:
            output = (FIXTURES / "docker-stats-macos.jsonl").read_text(encoding="utf-8")
        elif "ps" in command:
            output = json.dumps(
                {
                    "ID": "be3b8bd3cf84",
                    "Names": "hrr-fixture-mem",
                    "Labels": "com.docker.compose.project=trial-a__abc__env",
                }
            )
        else:
            output = ""
        return SimpleNamespace(stdout=output)

    fd = os.open(run_dir / "memory-watch.jsonl", os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
    try:
        sample_once(run_dir, fd, "kernel", now=lambda: 10.0, run=run)
    finally:
        os.close(fd)

    rows = read_rows(run_dir / "memory-watch.jsonl").rows
    assert [row["type"] for row in rows] == ["sample", "kill"]
    assert all(row["trial"] == "trial-a__abc" for row in rows)
    assert all(row["used_mb"] == 3.758 and row["limit_mb"] == 3 for row in rows)
    assert all(row["location"] == "harbor-job" for row in rows)
    assert any(command[:2] == ["docker", "kill"] for command in commands)


def test_canary_container_is_sampled_and_killed_above_canary_limit(tmp_path: Path):
    (tmp_path / "machine-check.json").write_text(
        json.dumps(
            {
                "created_t": 1,
                "tool_version": "1",
                "harbor_version": "0.23.0",
                "host_fingerprint": "host",
                "task_source": {},
                "concurrency": 1,
                "tasks": [],
                "included": [],
                "verifier_timeout_multiplier": 1.0,
            }
        ),
        encoding="utf-8",
    )
    trial_dir = tmp_path / "canary-job" / "canary__alpha"
    trial_dir.mkdir(parents=True)
    (trial_dir / "config.json").write_text(
        json.dumps(
            {
                "trial_name": "canary__alpha",
                "task": {"name": "canary/say-hello"},
                "agent": {"kwargs": {"arm_label": "arm-a"}},
            }
        ),
        encoding="utf-8",
    )
    commands = []

    def run(command):
        commands.append(command)
        if "stats" in command:
            output = '{"Container":"hrr-fixture-mem","MemUsage":"513MiB / 1GiB"}\n'
        elif "ps" in command:
            output = json.dumps(
                {
                    "ID": "be3b8bd3cf84",
                    "Names": "hrr-fixture-mem",
                    "Labels": "com.docker.compose.project=canary__alpha__env",
                }
            )
        else:
            output = ""
        return SimpleNamespace(stdout=output)

    fd = os.open(tmp_path / "memory-watch.jsonl", os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
    try:
        sample_once(tmp_path, fd, "kernel", now=lambda: 10.0, run=run)
    finally:
        os.close(fd)

    rows = read_rows(tmp_path / "memory-watch.jsonl").rows
    assert [row["type"] for row in rows] == ["sample", "kill"]
    assert all(row["location"] == "canary-job" for row in rows)
    assert all(row["limit_mb"] == canary_memory_mb() for row in rows)
    assert any(command[:2] == ["docker", "kill"] for command in commands)


def test_sample_skips_container_that_disappears_during_inspect(tmp_path: Path):
    (tmp_path / "machine-check.json").write_text(
        json.dumps(
            {
                "created_t": 1,
                "tool_version": "1",
                "harbor_version": "0.23.0",
                "host_fingerprint": "host",
                "task_source": {},
                "concurrency": 1,
                "tasks": [{"name": "task-a", "memory_mb": 10}],
                "included": ["task-a"],
                "verifier_timeout_multiplier": 1.0,
            }
        ),
        encoding="utf-8",
    )
    for trial in ("trial-gone", "trial-healthy"):
        folder = tmp_path / "harbor-job" / trial
        folder.mkdir(parents=True)
        (folder / "config.json").write_text(
            json.dumps(
                {
                    "trial_name": trial,
                    "task": {"path": "task-a"},
                    "agent": {"kwargs": {"arm_label": "arm-a"}},
                }
            ),
            encoding="utf-8",
        )
    containers = [
        {
            "ID": "gone",
            "Labels": "com.docker.compose.project=trial-gone__env",
        },
        {
            "ID": "healthy",
            "Labels": "com.docker.compose.project=trial-healthy__env",
        },
    ]

    def run(command):
        if command[1] == "ps":
            return SimpleNamespace(stdout="\n".join(json.dumps(row) for row in containers))
        if command[1] == "stats":
            return SimpleNamespace(stdout='{"Container":"healthy","MemUsage":"1MiB / 10MiB"}')
        if command[1] == "inspect":
            raise AssertionError("proc reader should not be used for kernel memory")
        return SimpleNamespace(stdout="")

    fd = os.open(tmp_path / "memory-watch.jsonl", os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
    try:
        sample_once(tmp_path, fd, "kernel", now=lambda: 10.0, run=run)
    finally:
        os.close(fd)

    rows = read_rows(tmp_path / "memory-watch.jsonl").rows
    assert [(row["trial"], row["type"]) for row in rows] == [("trial-healthy", "sample")]


def test_sample_skips_container_when_kill_fails_without_marking_it_killed(tmp_path: Path):
    (tmp_path / "machine-check.json").write_text(
        json.dumps(
            {
                "created_t": 1,
                "tool_version": "1",
                "harbor_version": "0.23.0",
                "host_fingerprint": "host",
                "task_source": {},
                "concurrency": 1,
                "tasks": [{"name": "task-a", "memory_mb": 1}],
                "included": ["task-a"],
                "verifier_timeout_multiplier": 1.0,
            }
        ),
        encoding="utf-8",
    )
    for trial in ("trial-gone", "trial-healthy"):
        folder = tmp_path / "harbor-job" / trial
        folder.mkdir(parents=True)
        (folder / "config.json").write_text(
            json.dumps(
                {
                    "trial_name": trial,
                    "task": {"path": "task-a"},
                    "agent": {"kwargs": {"arm_label": "arm-a"}},
                }
            ),
            encoding="utf-8",
        )
    containers = [
        {
            "ID": "gone",
            "Labels": "com.docker.compose.project=trial-gone__env",
        },
        {
            "ID": "healthy",
            "Labels": "com.docker.compose.project=trial-healthy__env",
        },
    ]
    killed = set()

    def run(command):
        if command[1] == "ps":
            return SimpleNamespace(stdout="\n".join(json.dumps(row) for row in containers))
        if command[1] == "stats":
            return SimpleNamespace(
                stdout=(
                    '{"Container":"gone","MemUsage":"2MiB / 10MiB"}\n'
                    '{"Container":"healthy","MemUsage":"1MiB / 10MiB"}'
                )
            )
        if command[1] == "kill":
            raise OSError("container disappeared before kill")
        return SimpleNamespace(stdout="")

    fd = os.open(tmp_path / "memory-watch.jsonl", os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
    try:
        sample_once(tmp_path, fd, "kernel", now=lambda: 10.0, run=run, killed=killed)
    finally:
        os.close(fd)

    rows = read_rows(tmp_path / "memory-watch.jsonl").rows
    assert [(row["trial"], row["type"]) for row in rows] == [("trial-healthy", "sample")]
    assert killed == set()


def test_watcher_stops_when_the_runner_lifeline_closes(tmp_path: Path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        "harbor_research_runner.run.memory_watch.sample_once",
        lambda *args, **kwargs: calls.append(1),
    )
    read_fd, write_fd = os.pipe()
    os.close(write_fd)

    def wait(stopped, timeout):
        assert stopped.wait(1.0), "watcher did not see the closed lifeline"

    assert run_memory_watch(tmp_path, "kernel", read_fd, wait=wait) == 0
    assert len(calls) <= 1
