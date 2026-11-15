from __future__ import annotations

import json
import logging
import os
import shlex
import socket
import urllib.error
import urllib.request
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from harbor_research_runner.docker_host.cleanup import Inventory
from harbor_research_runner.docker_host.host_lock import HostLock
from harbor_research_runner.harbor_adapter.task_files import task_content_hash
from harbor_research_runner.records.machine_check import MachineCheck
from harbor_research_runner.records.resource_stats import Sample
from harbor_research_runner.run.checks import CheckDeps
from harbor_research_runner.run.loop import RunDeps, run_job
from harbor_research_runner.run.tool_identity import ToolIdentity
from tests.support.docker_stub import stub_docker_dir
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
    tasks_yaml: str = "tasks: {dataset: bench/tasks}\n",
    task_source: dict | None = None,
    task_digest: str = "sha256:task",
) -> tuple[Path, Path, Path, RunDeps, int]:
    port = _free_port()
    machine_check_path = tmp_path / "machine-check.json"
    job_path = tmp_path / "job.yaml"
    job_path.write_text(
        "name: study\n"
        "model: provider/model\n"
        "repetitions: 1\n"
        "concurrency: 1\n"
        "task_order: as_listed\n" + tasks_yaml + "machine_check: machine-check.json\n"
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
        task_source=task_source or {"dataset": "bench/tasks"},
        concurrency=1,
        tasks=[
            {"name": "task-a", "digest": task_digest, "memory_mb": 512},
            {"name": "canary/say-hello", "digest": "sha256:canary", "memory_mb": 1024},
        ],
        included=["task-a"],
        verifier_timeout_multiplier=1.0,
    )
    machine_check_path.write_text(machine_check.to_json(), encoding="utf-8")
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


def _expected_canary_argv(run_dir: Path, port: int) -> list[str]:
    return [
        "uv",
        "run",
        "harbor",
        "run",
        "--config",
        str(run_dir / "canary-job-config.yaml"),
        "--jobs-dir",
        str(run_dir),
        "--job-name",
        "canary-job",
        "--plugin",
        "harbor_research_runner.harbor_adapter.plugin:RunPlugin",
        "--pk",
        f"proxy_url=http://127.0.0.1:{port}",
        "--pk",
        f"limits_file={run_dir / 'proxy-config.json'}",
        "--pk",
        "task_order=as_listed",
    ]


def test_run_job_refuses_failed_checks_without_starting_services(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    upstream, upstream_url = _start_upstream()
    harbor_calls: list[tuple[list[str], Path, int]] = []
    job_path, manifest_path, runs_dir, deps, port = _run_deps(tmp_path, upstream_url, harbor_calls)
    docker_calls = tmp_path / "docker-calls.txt"
    monkeypatch.setenv(
        "PATH", f"{stub_docker_dir(tmp_path, docker_calls)}{os.pathsep}{os.environ['PATH']}"
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
    manifest_path.write_text(
        '[fake."' + "a" * 40 + '"]\n'
        'version = "fixture"\nwithdrawn = false\n'
        '[fake."' + "a" * 40 + '".images.arm64]\n'
        'ref = "hrr/fake:test"\n'
        'digest = "sha256:' + "0" * 64 + '"\n'
        'build_method = "source"\nbuilt_at = "2026-11-15"\n',
        encoding="utf-8",
    )
    docker_calls = tmp_path / "docker-calls.txt"
    monkeypatch.setenv(
        "PATH", f"{stub_docker_dir(tmp_path, docker_calls)}{os.pathsep}{os.environ['PATH']}"
    )
    try:
        result = run_job(job_path, runs_dir, manifest_path, {}, deps)
    finally:
        upstream.stop()

    assert result.exit_code == 1
    assert result.run_dir is not None
    assert len(harbor_calls) == 1
    argv, cwd, lifeline_fd = harbor_calls[0]
    assert argv == _expected_canary_argv(result.run_dir, port)
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
    assert canary_config["n_attempts"] == 1
    assert canary_config["verifier_timeout_multiplier"] == 1.0
    assert canary_config["datasets"][0]["task_names"] == ["say-hello"]
    agent = canary_config["agents"][0]
    assert agent["import_path"] == "harbor_research_runner.harness.fake:FakeHarnessAgent"
    assert agent["kwargs"]["arm_label"] == "arm-a"
    assert agent["kwargs"]["harness"] == "fake"
    assert agent["kwargs"]["commit"] == "a" * 40
    assert agent["kwargs"]["manifest_path"] == str(manifest_path)
    assert agent["kwargs"]["container_host"] == "127.0.0.1"
    assert agent["kwargs"]["proxy_port"] == port
    assert agent["kwargs"]["fake"] == {}
    metadata = json.loads((result.run_dir / "run-metadata.json").read_text(encoding="utf-8"))
    assert metadata["arms"][0]["image_ref"] == "hrr/fake:test"
    assert metadata["arms"][0]["image_digest"] == "sha256:" + "0" * 64
    assert metadata["task_set"]["ref"] == "bench/tasks"
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
        "PATH", f"{stub_docker_dir(tmp_path, docker_calls)}{os.pathsep}{os.environ['PATH']}"
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


def test_run_job_rejects_a_canary_with_two_trials_for_one_arm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    upstream, upstream_url = _start_upstream()
    harbor_calls: list[tuple[list[str], Path, int]] = []
    job_path, manifest_path, runs_dir, deps, port = _run_deps(
        tmp_path, upstream_url, harbor_calls, returncode=0
    )
    original_start_harbor = deps.start_harbor

    def start_harbor(argv: list[str], cwd: Path, lifeline_fd: int) -> HarborProcess:
        process = original_start_harbor(argv, cwd, lifeline_fd)
        fixture = Path(__file__).parents[2] / "fixtures/run_folders/clean/canary-job/canary-alpha"
        canary_dir = cwd / "canary-job"
        for name in ("canary-alpha", "canary-beta"):
            trial_dir = canary_dir / name
            trial_dir.mkdir(parents=True)
            config = json.loads((fixture / "config.json").read_text(encoding="utf-8"))
            config["agent"]["kwargs"]["arm_label"] = "arm-a"
            config["trial_name"] = name
            result_data = json.loads((fixture / "result.json").read_text(encoding="utf-8"))
            result_data["trial_name"] = name
            (trial_dir / "config.json").write_text(json.dumps(config), encoding="utf-8")
            (trial_dir / "result.json").write_text(json.dumps(result_data), encoding="utf-8")
        return process

    deps = replace(deps, start_harbor=start_harbor)
    docker_calls = tmp_path / "docker-calls.txt"
    monkeypatch.setenv(
        "PATH", f"{stub_docker_dir(tmp_path, docker_calls)}{os.pathsep}{os.environ['PATH']}"
    )
    try:
        result = run_job(job_path, runs_dir, manifest_path, {}, deps)
    finally:
        upstream.stop()

    assert result.exit_code == 1
    assert result.run_dir is not None
    assert "arm arm-a has 2 canary trials; expected exactly one" in (
        result.run_dir / "runner.log"
    ).read_text(encoding="utf-8")
    assert not (result.run_dir / "main-job-config.yaml").exists()
    _assert_proxy_stopped(port)


def test_run_job_reports_live_memory_and_oom_watch_services(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    upstream, upstream_url = _start_upstream()
    harbor_calls: list[tuple[list[str], Path, int]] = []
    job_path, manifest_path, runs_dir, deps, port = _run_deps(
        tmp_path, upstream_url, harbor_calls, returncode=7
    )
    observed: list[tuple[bool, bool]] = []

    def start_services(session, restart: bool) -> None:
        session.start_services(restart)
        observed.append((session.memory_watch_alive(), session.oom_watch_alive()))

    deps = replace(deps, start_services=start_services)
    docker_calls = tmp_path / "docker-calls.txt"
    monkeypatch.setenv(
        "PATH", f"{stub_docker_dir(tmp_path, docker_calls)}{os.pathsep}{os.environ['PATH']}"
    )
    try:
        result = run_job(job_path, runs_dir, manifest_path, {}, deps)
    finally:
        upstream.stop()

    assert result.exit_code == 1
    assert observed == [(True, True)]
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
        "PATH", f"{stub_docker_dir(tmp_path, docker_calls)}{os.pathsep}{os.environ['PATH']}"
    )
    try:
        result = run_job(job_path, runs_dir, manifest_path, {}, deps)
    finally:
        upstream.stop()

    assert result.exit_code == 1
    assert result.run_dir is not None
    assert len(harbor_calls) == 1
    argv, cwd, lifeline_fd = harbor_calls[0]
    assert argv == _expected_canary_argv(result.run_dir, port)
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


@pytest.mark.parametrize("source_kind", ["path", "git"])
def test_run_job_records_path_and_git_task_references(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source_kind: str
) -> None:
    if source_kind == "path":
        task_root = tmp_path / "tasks"
        task_dir = task_root / "task-a"
        task_dir.mkdir(parents=True)
        (task_dir / "task.toml").write_text('version = "1.0"\n', encoding="utf-8")
        task_digest = task_content_hash(task_dir)
        tasks_yaml = f"tasks: {{path: {task_root}}}\n"
        task_source = {"path": str(task_root)}
        expected_ref = str(task_root)
    else:
        commit = "a" * 40
        url = "https://example.invalid/tasks.git"
        task_digest = "sha256:task"
        tasks_yaml = f"tasks: {{git: {{url: {url}, commit: {commit}}}}}\n"
        task_source = {"git": {"url": url, "commit": commit}}
        expected_ref = f"{url}@{commit}"

    upstream, upstream_url = _start_upstream()
    harbor_calls: list[tuple[list[str], Path, int]] = []
    job_path, manifest_path, runs_dir, deps, _port = _run_deps(
        tmp_path,
        upstream_url,
        harbor_calls,
        returncode=7,
        tasks_yaml=tasks_yaml,
        task_source=task_source,
        task_digest=task_digest,
    )
    docker_calls = tmp_path / "docker-calls.txt"
    monkeypatch.setenv(
        "PATH", f"{stub_docker_dir(tmp_path, docker_calls)}{os.pathsep}{os.environ['PATH']}"
    )
    try:
        result = run_job(job_path, runs_dir, manifest_path, {}, deps)
    finally:
        upstream.stop()

    assert result.exit_code == 1
    assert result.run_dir is not None
    metadata = json.loads((result.run_dir / "run-metadata.json").read_text(encoding="utf-8"))
    assert metadata["task_set"]["ref"] == expected_ref
