from __future__ import annotations

import json
import logging
import os
import shlex
import socket
import urllib.error
import urllib.request
from pathlib import Path

import pytest
import yaml

from harbor_research_runner.docker_host.cleanup import Inventory
from harbor_research_runner.docker_host.host_lock import HostLock
from harbor_research_runner.records.machine_check import MachineCheck
from harbor_research_runner.records.resource_stats import Sample
from harbor_research_runner.run.checks import CheckDeps
from harbor_research_runner.run.loop import RunDeps, run_job
from harbor_research_runner.run.tool_identity import ToolIdentity
from tests.support.docker_stub import _stub_docker_dir
from tests.support.fake_upstream import FakeUpstream


_PROFILE = {
    "arch": "arm64",
    "cpus": 8,
    "docker_version": "27.0.0",
    "memory_cap": "kernel",
    "swap_limit": True,
    "rootless": False,
    "cgroup_version": "2",
    "docker_memory_mb": 32768,
    "container_network": "host",
}
_NOW = 1794480000.123456


class HarborProcess:
    def __init__(self, returncode: int) -> None:
        self.returncode = returncode

    def wait(self) -> int:
        return self.returncode


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _run_deps(
    tmp_path: Path,
    upstream_url: str,
    harbor_calls: list[tuple[list[str], Path, int]],
    *,
    returncode: int = 0,
    start_error: Exception | None = None,
) -> tuple[Path, Path, Path, RunDeps, int]:
    port = _free_port()
    machine_check_path = tmp_path / "machine-check.json"
    machine_check_path.write_text("{}", encoding="utf-8")
    job_path = tmp_path / "job.yaml"
    job_path.write_text(
        "name: study\n"
        "model: provider/model\n"
        "repetitions: 1\n"
        "concurrency: 1\n"
        "task_order: as_listed\n"
        "tasks: {dataset: bench/tasks}\n"
        "machine_check: machine-check.json\n"
        "arms: [{label: arm-a, harness: fake, commit: " + "a" * 40 + ", fake: {}}]\n"
        "proxy: {host: 127.0.0.1, port: " + str(port) + ", container_host: 127.0.0.1}\n"
        "limits: {}\n",
        encoding="utf-8",
    )
    provider_dir = tmp_path / "provider"
    provider_dir.mkdir()
    (provider_dir / "model.json").write_text(
        json.dumps(
            {
                "id": "provider/model",
                "upstream_model": "fake-model",
                "api": "chat",
                "endpoints": [f"{upstream_url}/v1/chat/completions"],
                "key_env": [],
                "tokens_per_minute": None,
                "pricing": {"input": 0, "output": 0, "cached_read": 0},
                "variants": {},
                "withdrawn": False,
                "reason": None,
            }
        ),
        encoding="utf-8",
    )
    manifest_path = tmp_path / "manifest.toml"
    manifest_path.write_text("", encoding="utf-8")
    machine_check = MachineCheck(
        created_t=1.0,
        tool_version="0.1.0",
        harbor_version="0.23.0",
        host_fingerprint="fingerprint",
        host_profile=_PROFILE,
        task_source={"dataset": "bench/tasks"},
        concurrency=1,
        tasks=[{"name": "task-a", "digest": "sha256:task", "memory_mb": 512}],
        included=["task-a"],
        verifier_timeout_multiplier=1.0,
    )
    host_lock = HostLock(tmp_path / "host.lock")
    checks = CheckDeps(
        select_adapter=lambda *_args: object(),
        measure=lambda **_kwargs: (Sample(1.0, 4096, 0, 100.0, 0.0, 0, True), 0),
        inventory=lambda: Inventory(True, "ok", (), ()),
        live_run=lambda _path: None,
        host_lock=host_lock,
        read_machine_check=lambda _path: machine_check,
        host_fingerprint=lambda: "fingerprint",
        harbor_version=lambda: "0.23.0",
        docker_memory_mb=lambda: 32768,
        host_profile=lambda: _PROFILE,
        processes=lambda _run_dir: [],
    )

    def start_harbor(argv: list[str], cwd: Path, lifeline_fd: int) -> HarborProcess:
        os.fstat(lifeline_fd)
        harbor_calls.append((argv, cwd, lifeline_fd))
        if start_error is not None:
            raise start_error
        return HarborProcess(returncode)

    deps = RunDeps(
        checks=checks,
        tool=ToolIdentity("0.1.0", "a" * 40),
        host=lambda: {
            "fingerprint": "fingerprint",
            "hostname": "test-node",
            "host_profile": _PROFILE,
            "arch": "arm64",
            "cpus": 8,
            "memory_mb": 32768,
            "docker_version": "27.0.0",
        },
        start_harbor=start_harbor,
        watch=lambda *_args: None,
        start_services=lambda session, restart: session.start_services(restart),
        stop_services=lambda session: session.stop_services(),
        now=lambda: _NOW,
    )
    return job_path, manifest_path, tmp_path / "runs", deps, port


def _start_upstream() -> tuple[FakeUpstream, str]:
    upstream = FakeUpstream(["ok"])
    return upstream, upstream.start()


def _assert_proxy_stopped(port: int) -> None:
    with pytest.raises(urllib.error.URLError):
        urllib.request.urlopen(f"http://127.0.0.1:{port}/control/health", timeout=0.5)


def test_run_job_refuses_failed_checks_without_starting_services(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    upstream, upstream_url = _start_upstream()
    harbor_calls: list[tuple[list[str], Path, int]] = []
    job_path, manifest_path, runs_dir, deps, port = _run_deps(tmp_path, upstream_url, harbor_calls)
    docker_calls = tmp_path / "docker-calls.txt"
    monkeypatch.setenv(
        "PATH", f"{_stub_docker_dir(tmp_path, docker_calls)}{os.pathsep}{os.environ['PATH']}"
    )
    holder = HostLock(tmp_path / "host.lock")
    holder.acquire()
    try:
        result = run_job(job_path, runs_dir, manifest_path, {}, deps)
    finally:
        holder.release()
        upstream.stop()

    assert result.exit_code == 1
    assert result.run_dir is not None
    assert harbor_calls == []
    assert not docker_calls.exists()
    events = [
        json.loads(line)
        for line in (result.run_dir / "run-events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    check = next(
        row for row in events if row["type"] == "launch_check" and row["name"] == "single_run"
    )
    assert check["ok"] is False
    assert "host lock is held" in check["detail"]
    assert "single_run: host lock is held" in caplog.text
    _assert_proxy_stopped(port)


def test_run_job_records_nonzero_canary_exit_and_stops_services(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    upstream, upstream_url = _start_upstream()
    harbor_calls: list[tuple[list[str], Path, int]] = []
    job_path, manifest_path, runs_dir, deps, port = _run_deps(
        tmp_path, upstream_url, harbor_calls, returncode=7
    )
    docker_calls = tmp_path / "docker-calls.txt"
    monkeypatch.setenv(
        "PATH", f"{_stub_docker_dir(tmp_path, docker_calls)}{os.pathsep}{os.environ['PATH']}"
    )
    try:
        result = run_job(job_path, runs_dir, manifest_path, {}, deps)
    finally:
        upstream.stop()

    assert result.exit_code == 1
    assert result.run_dir is not None
    assert len(harbor_calls) == 1
    argv, cwd, lifeline_fd = harbor_calls[0]
    assert cwd == result.run_dir
    assert f"proxy_url=http://127.0.0.1:{port}" in argv
    assert "task_order=as_listed" in argv
    events = [
        json.loads(line)
        for line in (result.run_dir / "run-events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert [row for row in events if row["type"] == "harbor_start"] == [
        {
            "type": "harbor_start",
            "t": _NOW,
            "pass": 0,
            "command": shlex.join(argv),
            "job": "canary",
        }
    ]
    assert [row for row in events if row["type"] == "harbor_exit"] == [
        {"type": "harbor_exit", "t": _NOW, "pass": 0, "returncode": 7, "job": "canary"}
    ]
    run_log = (result.run_dir / "runner.log").read_text(encoding="utf-8")
    assert "canary failed with Harbor exit code 7:" in run_log
    assert "run failed" not in run_log
    canary_config = yaml.safe_load(
        (result.run_dir / "canary-job-config.yaml").read_text(encoding="utf-8")
    )
    assert canary_config["job_name"] == "canary-job"
    assert (result.run_dir / "compose-host-network.yaml").is_file()
    compose = yaml.safe_load(
        (result.run_dir / "compose-host-network.yaml").read_text(encoding="utf-8")
    )
    assert compose["services"]["main"]["network_mode"] == "host"
    _assert_proxy_stopped(port)


def test_run_job_rejects_successful_canary_without_a_trial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    upstream, upstream_url = _start_upstream()
    harbor_calls: list[tuple[list[str], Path, int]] = []
    job_path, manifest_path, runs_dir, deps, port = _run_deps(
        tmp_path, upstream_url, harbor_calls, returncode=0
    )
    docker_calls = tmp_path / "docker-calls.txt"
    monkeypatch.setenv(
        "PATH", f"{_stub_docker_dir(tmp_path, docker_calls)}{os.pathsep}{os.environ['PATH']}"
    )
    try:
        result = run_job(job_path, runs_dir, manifest_path, {}, deps)
    finally:
        upstream.stop()

    assert result.exit_code == 1
    assert result.run_dir is not None
    assert len(harbor_calls) == 1
    assert not (result.run_dir / "canary-job").exists()
    events = [
        json.loads(line)
        for line in (result.run_dir / "run-events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert [row for row in events if row["type"] == "harbor_exit"] == [
        {"type": "harbor_exit", "t": _NOW, "pass": 0, "returncode": 0, "job": "canary"}
    ]
    run_log = (result.run_dir / "runner.log").read_text(encoding="utf-8")
    assert "canary failed with Harbor exit code 0:" in run_log
    assert "run failed" not in run_log
    _assert_proxy_stopped(port)


def test_run_job_stops_services_and_releases_the_lock_when_harbor_start_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    upstream, upstream_url = _start_upstream()
    harbor_calls: list[tuple[list[str], Path, int]] = []
    job_path, manifest_path, runs_dir, deps, port = _run_deps(
        tmp_path,
        upstream_url,
        harbor_calls,
        start_error=RuntimeError("Harbor start failed"),
    )
    docker_calls = tmp_path / "docker-calls.txt"
    monkeypatch.setenv(
        "PATH", f"{_stub_docker_dir(tmp_path, docker_calls)}{os.pathsep}{os.environ['PATH']}"
    )
    try:
        result = run_job(job_path, runs_dir, manifest_path, {}, deps)
    finally:
        upstream.stop()

    assert result.exit_code == 1
    assert result.run_dir is not None
    assert len(harbor_calls) == 1
    argv, cwd, lifeline_fd = harbor_calls[0]
    assert f"proxy_url=http://127.0.0.1:{port}" in argv
    assert "task_order=as_listed" in argv
    assert cwd == result.run_dir
    lock = HostLock(tmp_path / "host.lock")
    lock.acquire()
    lock.release()
    log_path = result.run_dir / "runner.log"
    before = log_path.read_bytes()
    logging.getLogger("harbor_research_runner.run.loop").warning("after run")
    assert log_path.read_bytes() == before
    _assert_proxy_stopped(port)
