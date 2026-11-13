from __future__ import annotations

import json
import os
import socket
import time
import urllib.request
from pathlib import Path

import pytest

from harbor_research_runner.job_config.job_file import Arm, Job, TaskSource
from harbor_research_runner.job_config.model_cards import ModelCard
from harbor_research_runner.records.machine_check import MachineCheck
from harbor_research_runner.run.checks import CheckResult
from harbor_research_runner.run.loop import RunSession
from harbor_research_runner.run.tool_identity import ToolIdentity
from tests.support.fake_upstream import FakeUpstream


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _session(tmp_path: Path, upstream_url: str) -> RunSession:
    job_path = tmp_path / "job.yaml"
    job_path.write_text("name: study\n", encoding="utf-8")
    machine_check_path = tmp_path / "machine-check.json"
    machine_check_path.write_text("{}", encoding="utf-8")
    manifest_path = tmp_path / "manifest.toml"
    manifest_path.write_text("", encoding="utf-8")
    job = Job(
        path=job_path,
        name="integration-study",
        model="fake/model",
        repetitions=1,
        concurrency=1,
        task_order="as_listed",
        tasks=TaskSource("dataset", "bench/tasks", None, None, None, (), ()),
        machine_check=machine_check_path,
        arms=(Arm("arm-a", "fake", "a" * 40),),
        proxy_host="127.0.0.1",
        proxy_port=_free_port(),
        container_host="host.docker.internal",
        limit_overrides={},
    )
    card = ModelCard(
        id="fake/model",
        upstream_model="fake-model",
        api="chat",
        endpoints=[f"{upstream_url}/v1/chat/completions"],
        key_env=[],
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
    return RunSession.create(
        job=job,
        card=card,
        manifest_path=manifest_path,
        runs_dir=tmp_path / "runs",
        environment={},
        check_results=[CheckResult("ready", True, "ok")],
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
        host={
            "fingerprint": "fingerprint",
            "arch": "arm64",
            "cpus": 8,
            "memory_mb": 32768,
            "docker_version": "27.0.0",
        },
    )


def test_refused_resume_appends_no_event(tmp_path: Path) -> None:
    session = _session(tmp_path, "http://127.0.0.1:1")
    events_path = session.run_dir / "run-events.jsonl"
    before = events_path.read_bytes()

    RunSession.load(session.run_dir, {})

    assert events_path.read_bytes() == before


def test_resume_does_not_refuse_itself(tmp_path: Path) -> None:
    session = _session(tmp_path, "http://127.0.0.1:1")
    resumed = RunSession.load(session.run_dir, {})

    resumed.mark_resumed(lambda: 1794480100.5)

    last_event = json.loads((session.run_dir / "run-events.jsonl").read_text().splitlines()[-1])
    assert last_event["type"] == "resume"
    assert last_event["tool_commit"] == "a" * 40


def test_services_start_sample_and_stop(tmp_path: Path) -> None:
    upstream = FakeUpstream(["ok"])
    upstream_url = upstream.start()
    session = _session(tmp_path, upstream_url)
    try:
        session.start_services(restart=False)
        with urllib.request.urlopen(
            f"http://{session.job.proxy_host}:{session.job.proxy_port}/control/health", timeout=1
        ) as response:
            assert response.status == 200
        deadline = time.monotonic() + 4
        stats_path = session.run_dir / "resource-stats.jsonl"
        while time.monotonic() < deadline and (
            not stats_path.exists() or not stats_path.read_text(encoding="utf-8").strip()
        ):
            time.sleep(0.02)
        assert stats_path.exists()
        assert stats_path.read_text(encoding="utf-8").strip()
        proxy_process = session._proxy
        sampler_process = session._sampler

        session.stop_services()

        assert proxy_process is not None and proxy_process.poll() is not None
        assert sampler_process is not None and sampler_process.poll() is not None
    finally:
        session.stop_services()
        upstream.stop()


def test_services_restart_records_both_proxy_starts_in_the_ledger(tmp_path: Path) -> None:
    upstream = FakeUpstream(["ok"])
    upstream_url = upstream.start()
    session = _session(tmp_path, upstream_url)
    try:
        session.start_services(restart=False)
        session.stop_services()
        session.start_services(restart=True)
        session.stop_services()
        ledger = [
            json.loads(line)
            for line in (session.run_dir / "proxy-ledger.jsonl").read_text().splitlines()
        ]
        starts = [row for row in ledger if row["type"] == "proxy_start"]
        assert [row["restart"] for row in starts] == [False, True]
    finally:
        session.stop_services()
        upstream.stop()


def test_service_lifeline_writer_is_non_inheritable(tmp_path: Path) -> None:
    upstream = FakeUpstream(["ok"])
    upstream_url = upstream.start()
    session = _session(tmp_path, upstream_url)
    try:
        session.start_services(restart=False)
        writer_fd = session._lifeline_write_fd

        assert writer_fd is not None
        assert not os.get_inheritable(writer_fd)
    finally:
        session.stop_services()
        upstream.stop()


def test_stopping_services_closes_lifeline_writer(tmp_path: Path) -> None:
    upstream = FakeUpstream(["ok"])
    upstream_url = upstream.start()
    session = _session(tmp_path, upstream_url)
    try:
        session.start_services(restart=False)
        writer_fd = session._lifeline_write_fd
        assert writer_fd is not None

        session.stop_services()

        with pytest.raises(OSError):
            os.fstat(writer_fd)
    finally:
        session.stop_services()
        upstream.stop()
