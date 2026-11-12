from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from harbor_research_runner.job_config.job_file import Arm, Job, TaskSource
from harbor_research_runner.job_config.model_cards import ModelCard
from harbor_research_runner.records.machine_check import MachineCheck
from harbor_research_runner.run.checks import CheckResult
from harbor_research_runner.run.loop import RefusedToStart, RunSession
from harbor_research_runner.run.tool_identity import ToolIdentity

_HOST = {
    "fingerprint": "fingerprint",
    "arch": "arm64",
    "cpus": 8,
    "memory_mb": 32768,
    "docker_version": "27.0.0",
}


def _inputs(tmp_path: Path):
    job_path = tmp_path / "job.yaml"
    job_path.write_text(
        "name: study\nmodel: provider/model\nrepetitions: 1\nconcurrency: 1\n"
        "task_order: as_listed\ntasks:\n  dataset: bench/tasks\n"
        "machine_check: machine-check.json\narms:\n  - {label: arm-a, harness: fake, commit: "
        + "'"
        + "a" * 40
        + "'}\nproxy: {host: 127.0.0.1, port: 8766, container_host: host.docker.internal}\n"
        "limits: {}\n",
        encoding="utf-8",
    )
    machine_check_path = tmp_path / "machine-check.json"
    machine_check_path.write_text("{}", encoding="utf-8")
    manifest_path = tmp_path / "harness-manifest.toml"
    manifest_path.write_text("", encoding="utf-8")
    job = Job(
        path=job_path,
        name="study",
        model="provider/model",
        repetitions=1,
        concurrency=1,
        task_order="as_listed",
        tasks=TaskSource("dataset", "bench/tasks", None, None, None, (), ()),
        machine_check=machine_check_path,
        arms=(Arm("arm-a", "fake", "a" * 40),),
        proxy_host="127.0.0.1",
        proxy_port=8766,
        container_host="host.docker.internal",
        limit_overrides={},
    )
    card = ModelCard(
        id="provider/model",
        upstream_model="upstream-model",
        api="chat",
        endpoints=["https://model.invalid/v1"],
        key_env=["MODEL_KEY"],
        tokens_per_minute=None,
        pricing={"input": 0, "output": 0, "cached_read": 0},
        variants={},
        withdrawn=False,
        reason=None,
    )
    machine_check = MachineCheck(
        created_t=1,
        tool_version="0.1.0",
        harbor_version="0.23.0",
        host_fingerprint="fingerprint",
        task_source={"dataset": "bench/tasks"},
        concurrency=1,
        tasks=[{"name": "task-a", "digest": "sha256:task"}],
        included=["task-a"],
        verifier_timeout_multiplier=1.0,
    )
    return job, card, manifest_path, machine_check


def _create(tmp_path: Path, check_results: list[CheckResult] | None = None) -> RunSession:
    job, card, manifest_path, machine_check = _inputs(tmp_path)
    return RunSession.create(
        job=job,
        card=card,
        manifest_path=manifest_path,
        runs_dir=tmp_path / "runs",
        environment={"MODEL_KEY": "configured"},
        check_results=check_results or [CheckResult("ready", True, "ok")],
        tasks={"ref": "bench/tasks", "tasks": [{"name": "task-a", "digest": "sha256:task"}]},
        arms=[
            {
                "label": "arm-a",
                "harness": "fake",
                "commit": "a" * 40,
                "image_ref": "",
                "image_digest": "",
                "build_method": "",
            }
        ],
        machine_check=machine_check,
        now=lambda: 1794480000.123456,
        tool=ToolIdentity("0.1.0", "a" * 40),
        host=_HOST,
    )


def test_create_writes_launch_checks_and_metadata(tmp_path: Path) -> None:
    session = _create(tmp_path)

    events = [
        json.loads(line) for line in (session.run_dir / "run-events.jsonl").read_text().splitlines()
    ]
    metadata = json.loads((session.run_dir / "run-metadata.json").read_text())
    assert [event["type"] for event in events] == ["launch", "launch_check"]
    assert events[0]["tool_commit"] == "a" * 40
    assert events[1]["name"] == "ready"
    assert metadata["host"] == _HOST
    assert metadata["tool_version"] == "0.1.0"


def test_create_keeps_the_checked_machine_check_in_the_run_folder(tmp_path: Path) -> None:
    session = _create(tmp_path)
    source = session.job.machine_check
    copy = session.run_dir / "machine-check.json"

    source.write_text('{"changed": true}', encoding="utf-8")

    metadata = json.loads((session.run_dir / "run-metadata.json").read_text())
    assert copy.read_bytes() == b"{}"
    assert hashlib.sha256(copy.read_bytes()).hexdigest() == metadata["machine_check_sha256"]


def test_create_refuses_failed_checks_without_metadata(tmp_path: Path) -> None:
    with pytest.raises(RefusedToStart):
        _create(tmp_path, [CheckResult("ready", False, "not ready")])

    run_dir = next((tmp_path / "runs").iterdir())
    events = [json.loads(line) for line in (run_dir / "run-events.jsonl").read_text().splitlines()]
    assert events[-1] == {
        "type": "launch_check",
        "t": 1794480000.123456,
        "name": "ready",
        "ok": False,
        "detail": "not ready",
    }
    assert not (run_dir / "run-metadata.json").exists()


def test_load_does_not_change_metadata_or_append_event(tmp_path: Path) -> None:
    session = _create(tmp_path)
    metadata_path = session.run_dir / "run-metadata.json"
    before = metadata_path.read_bytes()
    events_before = (session.run_dir / "run-events.jsonl").read_bytes()

    loaded = RunSession.load(session.run_dir, {"MODEL_KEY": "configured"})

    assert loaded.run_dir == session.run_dir
    assert metadata_path.read_bytes() == before
    assert (session.run_dir / "run-events.jsonl").read_bytes() == events_before


def test_mark_resumed_appends_resume_event(tmp_path: Path) -> None:
    session = _create(tmp_path)

    session.mark_resumed(lambda: 1794480100.5)

    event = json.loads((session.run_dir / "run-events.jsonl").read_text().splitlines()[-1])
    assert event == {
        "type": "resume",
        "t": 1794480100.5,
        "tool_version": "0.1.0",
        "tool_commit": "a" * 40,
        "pid": __import__("os").getpid(),
    }
