import json
import os
import subprocess
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
    assert [row["type"] for row in rows] == ["sample", "over_limit", "kill"]
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
            used = canary_memory_mb() + 1
            output = f'{{"Container":"hrr-fixture-mem","MemUsage":"{used}MiB / 4GiB"}}\n'
        elif "ps" in command:
            output = json.dumps(
                {
                    "ID": "be3b8bd3cf84",
                    "Names": "hrr-fixture-mem",
                    "Labels": "com.docker.compose.project=canary__alpha__env",
                }
            )
        elif command[1] == "kill":
            output = ""
        else:
            output = "PID RSS COMMAND\n"
        return SimpleNamespace(stdout=output)

    fd = os.open(tmp_path / "memory-watch.jsonl", os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
    try:
        sample_once(tmp_path, fd, "kernel", now=lambda: 10.0, run=run)
    finally:
        os.close(fd)

    rows = read_rows(tmp_path / "memory-watch.jsonl").rows
    assert [row["type"] for row in rows] == ["sample", "over_limit", "kill"]
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


def test_sample_records_breach_when_kill_fails_and_does_not_retry(tmp_path: Path):
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
            raise subprocess.CalledProcessError(1, command, stderr="kill failed")
        if command[1] == "top":
            return SimpleNamespace(stdout="PID RSS COMMAND\n")
        return SimpleNamespace(stdout="")

    fd = os.open(tmp_path / "memory-watch.jsonl", os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
    try:
        sample_once(tmp_path, fd, "kernel", now=lambda: 10.0, run=run, killed=killed)
    finally:
        os.close(fd)

    rows = read_rows(tmp_path / "memory-watch.jsonl").rows
    assert [(row["trial"], row["type"]) for row in rows] == [
        ("trial-gone", "sample"),
        ("trial-gone", "over_limit"),
        ("trial-gone", "kill_failed"),
        ("trial-healthy", "sample"),
    ]
    assert rows[2]["error"]
    assert killed == {"gone"}

    fd = os.open(tmp_path / "memory-watch.jsonl", os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
    try:
        sample_once(tmp_path, fd, "kernel", now=lambda: 12.0, run=run, killed=killed)
    finally:
        os.close(fd)
    rows = read_rows(tmp_path / "memory-watch.jsonl").rows
    assert [row["type"] for row in rows[-2:]] == ["sample", "sample"]


def test_docker_stats_over_limit_row_lists_processes_from_docker_top(tmp_path: Path):
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
                "tasks": [{"name": "task-a", "memory_mb": 1}],
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
    top = (FIXTURES / "docker-top-macos.txt").read_text(encoding="utf-8")

    def run(command):
        if command[1] == "ps":
            return SimpleNamespace(
                stdout=json.dumps(
                    {
                        "ID": "be3b8bd3cf84",
                        "Names": "hrr-fixture-mem",
                        "Labels": "com.docker.compose.project=trial-a__abc__env",
                    }
                )
            )
        if command[1] == "stats":
            return SimpleNamespace(
                stdout='{"Container":"hrr-fixture-mem","MemUsage":"3MiB / 4GiB"}'
            )
        if command[1] == "top":
            return SimpleNamespace(stdout=top)
        return SimpleNamespace(stdout="")

    fd = os.open(run_dir / "memory-watch.jsonl", os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
    try:
        sample_once(run_dir, fd, "kernel", now=lambda: 10.0, run=run)
    finally:
        os.close(fd)

    rows = read_rows(run_dir / "memory-watch.jsonl").rows
    assert rows[1]["type"] == "over_limit"
    assert rows[1]["processes"] == [
        {"pid": 617, "cmd": "sleep 120", "mb": 816 / 1024},
        {"pid": 630, "cmd": "sleep 120", "mb": 812 / 1024},
    ]


def test_proc_over_limit_row_lists_processes_from_namespace_scan(tmp_path: Path):
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
                "tasks": [{"name": "task-a", "memory_mb": 1}],
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

    def run(command):
        if command[1] == "ps":
            return SimpleNamespace(
                stdout=json.dumps(
                    {"ID": "container-a", "Labels": "com.docker.compose.project=trial-a__abc__env"}
                )
            )
        if command[1] == "inspect":
            return SimpleNamespace(stdout='{"Pid":100}')
        return SimpleNamespace(stdout="")

    def read_text(path):
        path = str(path)
        if path.endswith("/100/smaps_rollup"):
            return "Pss: 1024 kB\n"
        if path.endswith("/200/smaps_rollup"):
            return "Pss: 2048 kB\n"
        if path.endswith("/100/cmdline"):
            return "python\0app.py\0"
        if path.endswith("/200/cmdline"):
            return "python\0worker.py\0"
        raise FileNotFoundError(path)

    def readlink(path):
        mapping = {
            "/proc/100/ns/pid": "pid:[4026532001]",
            "/proc/200/ns/pid": "pid:[4026532001]",
            "/proc/self/ns/pid": "pid:[4026531836]",
        }
        return mapping[str(path)]

    fd = os.open(run_dir / "memory-watch.jsonl", os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
    try:
        sample_once(
            run_dir,
            fd,
            "none",
            now=lambda: 10.0,
            run=run,
            list_dir=lambda _path: [Path("/proc/100"), Path("/proc/200")],
            read_text=read_text,
            readlink=readlink,
        )
    finally:
        os.close(fd)

    rows = read_rows(run_dir / "memory-watch.jsonl").rows
    assert rows[1]["type"] == "over_limit"
    assert rows[1]["processes"] == [
        {"pid": 200, "cmd": "python worker.py", "mb": 2.0},
        {"pid": 100, "cmd": "python app.py", "mb": 1.0},
    ]


def test_over_limit_row_records_process_listing_failure(tmp_path: Path):
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
                "tasks": [{"name": "task-a", "memory_mb": 1}],
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

    def run(command):
        if command[1] == "ps":
            return SimpleNamespace(
                stdout=json.dumps(
                    {"ID": "container-a", "Labels": "com.docker.compose.project=trial-a__abc__env"}
                )
            )
        if command[1] == "stats":
            return SimpleNamespace(stdout='{"Container":"container-a","MemUsage":"2MiB / 4GiB"}')
        if command[1] == "top":
            raise OSError("docker top unavailable")
        return SimpleNamespace(stdout="")

    fd = os.open(run_dir / "memory-watch.jsonl", os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
    try:
        sample_once(run_dir, fd, "kernel", now=lambda: 10.0, run=run)
    finally:
        os.close(fd)

    rows = read_rows(run_dir / "memory-watch.jsonl").rows
    assert rows[1]["type"] == "over_limit"
    assert rows[1]["processes"] is None
    assert rows[1]["processes_error"] == "docker top unavailable"
    assert rows[2]["type"] == "kill"


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


def _watch_run_dir(tmp_path: Path) -> Path:
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
    return tmp_path


def test_watcher_retries_docker_listing_failure_on_next_tick(tmp_path: Path, caplog):
    run_dir = _watch_run_dir(tmp_path)
    commands = []

    def run(command):
        commands.append(command)
        if len(commands) == 1:
            raise subprocess.CalledProcessError(1, command)
        return SimpleNamespace(stdout="")

    ticks = []

    def wait(stopped, timeout):
        ticks.append(timeout)
        if len(ticks) == 2:
            stopped.set()

    assert run_memory_watch(run_dir, "kernel", now=lambda: 10.0, wait=wait, run=run) == 0
    assert [command[1] for command in commands] == ["ps", "ps", "stats"]
    assert ticks == [2.0, 2.0]
    assert "memory watcher sample failed" in caplog.text


def test_watcher_exits_after_consecutive_docker_listing_failures(tmp_path: Path):
    run_dir = _watch_run_dir(tmp_path)
    commands = []

    def run(command):
        commands.append(command)
        raise subprocess.CalledProcessError(1, command)

    ticks = []

    def wait(stopped, timeout):
        ticks.append(timeout)

    assert run_memory_watch(run_dir, "kernel", now=lambda: 10.0, wait=wait, run=run) == 1
    assert len(commands) == 30
    assert len(ticks) == 29
