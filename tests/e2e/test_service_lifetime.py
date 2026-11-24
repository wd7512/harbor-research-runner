from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
import uuid
from contextlib import suppress
from pathlib import Path
from types import SimpleNamespace

import pytest

from harbor_research_runner.job_config.job_file import Arm, Job, TaskSource
from harbor_research_runner.job_config.model_cards import ModelCard
from harbor_research_runner.records.limits import Limits
from harbor_research_runner.run.loop import RunSession
from harbor_research_runner.run.memory_watch import sample_once
from harbor_research_runner.run.tool_identity import ToolIdentity

ROOT = Path(__file__).resolve().parents[2]


def _service_helper(run_dir: Path, port: int, pids_path: Path) -> int:
    run_dir.mkdir(parents=True, exist_ok=True)
    job = Job(
        path=run_dir / "job.yaml",
        name="service-lifetime",
        model="fake/model",
        repetitions=1,
        concurrency=1,
        task_order="as_listed",
        tasks=TaskSource("dataset", "bench/tasks", None, None, None, (), ()),
        machine_check=run_dir / "machine-check.json",
        arms=(Arm("arm-a", "fake", "a" * 40),),
        proxy_host="127.0.0.1",
        proxy_port=port,
        container_host="host.docker.internal",
        limit_overrides={},
    )
    card = ModelCard(
        id="fake/model",
        upstream_model="fake-model",
        api="chat",
        endpoints=["http://127.0.0.1:1/v1/chat/completions"],
        key_env=[],
        tokens_per_minute=None,
        pricing={"input": 0, "output": 0, "cached_read": 0},
        variants={},
        withdrawn=False,
        reason=None,
    )
    session = RunSession(
        run_dir,
        job,
        card,
        Limits.from_dict({}, repetitions=1, verifier_timeout_multiplier=1.0),
        SimpleNamespace(host={"host_profile": {"memory_cap": "kernel"}}),
        {},
        ToolIdentity("1.0.0", "a" * 40),
    )
    session.start_services(restart=False)
    pids_path.write_text(
        json.dumps(
            {
                "proxy": session._proxy.pid,
                "sampler": session._sampler.pid,
                "memory_watch": session._memory_watch.pid,
            }
        ),
        encoding="utf-8",
    )
    while True:
        time.sleep(1)


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def test_memory_watch_warning_and_kill_with_injected_docker_stats(tmp_path: Path) -> None:
    docker_info = subprocess.run(
        ["docker", "info", "--format", "{{.OSType}}"],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    if docker_info.returncode != 0:
        pytest.skip(f"Docker unavailable: {docker_info.stderr.strip()}")
    if docker_info.stdout.strip() != "linux":
        pytest.skip("Linux containers required")

    trial = f"memory-watch-{uuid.uuid4().hex[:12]}"
    container_name = f"hrr-{trial}"
    project = f"{trial}__env"
    image = (
        "python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b"
    )
    run_dir = tmp_path
    machine_check = {
        "created_t": 1,
        "tool_version": "1",
        "harbor_version": "0.23.0",
        "host_fingerprint": "test",
        "task_source": {},
        "concurrency": 1,
        "tasks": [{"name": "task-a", "memory_mb": 128}],
        "included": ["task-a"],
        "verifier_timeout_multiplier": 1.0,
    }
    (run_dir / "machine-check.json").write_text(json.dumps(machine_check), encoding="utf-8")
    (run_dir / "run-metadata.json").write_text(
        json.dumps(
            {
                "limits": Limits.from_dict(
                    {"memory_warn_multiplier": 1.25, "memory_kill_multiplier": 1.5},
                    repetitions=1,
                    verifier_timeout_multiplier=1.0,
                ).to_row()
            }
        ),
        encoding="utf-8",
    )
    trial_dir = run_dir / "harbor-job" / trial
    trial_dir.mkdir(parents=True)
    (trial_dir / "config.json").write_text(
        json.dumps(
            {
                "trial_name": trial,
                "task": {"path": "task-a"},
                "agent": {"kwargs": {"arm_label": "test-arm"}},
            }
        ),
        encoding="utf-8",
    )

    def docker(command: list[str], *, check: bool = True) -> subprocess.CompletedProcess:
        return subprocess.run(command, capture_output=True, text=True, check=check, timeout=60)

    def is_running() -> bool:
        state = docker(["docker", "inspect", "--format", "{{.State.Running}}", container_name])
        return state.stdout.strip() == "true"

    try:
        container_id = docker(
            [
                "docker",
                "run",
                "-d",
                "--name",
                container_name,
                "--label",
                f"com.docker.compose.project={project}",
                image,
                "sleep",
                "infinity",
            ]
        ).stdout.strip()
        output = run_dir / "memory-watch.jsonl"
        warned: set[str] = set()
        killed: set[str] = set()

        def sample(used_mb: int) -> list[dict]:
            def run(command: list[str]) -> subprocess.CompletedProcess:
                if command[:2] != ["docker", "stats"]:
                    return docker(command)
                stats = json.dumps(
                    {"Container": container_id[:12], "MemUsage": f"{used_mb}MiB / 1GiB"}
                )
                return subprocess.CompletedProcess(command, 0, stats, "")

            with output.open("a", encoding="utf-8") as stream:
                sample_once(
                    run_dir, stream.fileno(), "kernel", run=run, warned=warned, killed=killed
                )
            return [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]

        assert [row["type"] for row in sample(100)] == ["sample"]
        assert is_running()
        sample(170)
        warnings = [row for row in sample(170) if row["type"] != "sample"]
        assert [row["type"] for row in warnings] == ["warning"]
        assert (warnings[0]["warning_limit_mb"], warnings[0]["limit_mb"]) == (160, 192)
        assert is_running()
        assert [row["type"] for row in sample(220) if row["type"] != "sample"] == [
            "warning",
            "over_limit",
            "kill",
        ]
        assert not is_running()
    finally:
        docker(["docker", "rm", "-f", container_name], check=False)


def test_service_process_groups_exit_when_runner_is_killed(tmp_path: Path) -> None:
    with socket.socket() as reserve:
        reserve.bind(("127.0.0.1", 0))
        port = int(reserve.getsockname()[1])
    run_dir = tmp_path / "run"
    pids_path = tmp_path / "service-pids.json"
    helper = subprocess.Popen(
        [
            sys.executable,
            str(Path(__file__).resolve()),
            "--service-helper",
            str(run_dir),
            str(port),
            str(pids_path),
        ],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    groups: list[int] = []
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and not pids_path.exists() and helper.poll() is None:
            time.sleep(0.05)
        if not pids_path.exists():
            output = helper.communicate(timeout=2)[0]
            raise AssertionError(f"service helper did not start: {output}")
        process_ids = json.loads(pids_path.read_text(encoding="utf-8"))
        groups = [process_ids["proxy"], process_ids["sampler"], process_ids["memory_watch"]]
        helper.send_signal(signal.SIGKILL)
        helper.communicate(timeout=2)

        deadline = time.monotonic() + 20
        alive = [pgid for pgid in groups if _group_alive(pgid)]
        while alive and time.monotonic() < deadline:
            time.sleep(0.1)
            alive = [pgid for pgid in groups if _group_alive(pgid)]

        assert not alive
    finally:
        if helper.poll() is None:
            helper.kill()
            helper.communicate(timeout=2)
        for pgid in groups:
            if _group_alive(pgid):
                with suppress(ProcessLookupError):
                    os.killpg(pgid, signal.SIGTERM)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and any(_group_alive(pgid) for pgid in groups):
            time.sleep(0.05)
        for pgid in groups:
            if _group_alive(pgid):
                with suppress(ProcessLookupError):
                    os.killpg(pgid, signal.SIGKILL)


if __name__ == "__main__" and len(sys.argv) > 1 and sys.argv[1] == "--service-helper":
    raise SystemExit(_service_helper(Path(sys.argv[2]), int(sys.argv[3]), Path(sys.argv[4])))
