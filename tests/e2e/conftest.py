from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import time
import urllib.error
import urllib.request
from contextlib import suppress
from dataclasses import asdict, dataclass
from functools import cache
from importlib.metadata import version
from pathlib import Path

import pytest
import yaml

from harbor_research_runner.docker_host.processes import pids_naming_run
from harbor_research_runner.docker_host.profile import host_profile
from harbor_research_runner.docker_host.resources import host_fingerprint
from harbor_research_runner.harbor_adapter.commands import (
    job_config as harbor_job_config,
)
from harbor_research_runner.harbor_adapter.commands import (
    machine_check_command,
    resume_command,
    run_command,
    write_job_config,
)
from harbor_research_runner.harness.agents import agent_settings
from harbor_research_runner.job_config.job_file import load_job
from harbor_research_runner.job_config.model_cards import ModelCard
from harbor_research_runner.records.jsonl import read_rows
from harbor_research_runner.records.limits import Limits
from harbor_research_runner.records.machine_check import MachineCheck
from harbor_research_runner.records.machine_check import write as write_machine_check
from harbor_research_runner.records.proxy_control import ProxyConfig, write
from tests.support.fake_upstream import FakeUpstream
from tests.support.processes import ps_command

ROOT = Path(__file__).resolve().parents[2]
TASK = ROOT / "src/harbor_research_runner/run/canary_task/say-hello"
RUN_PROCESSES_STOP_WITHIN_S = 20


@dataclass
class HarborJob:
    run_dir: Path
    job_name: str
    proxy_url: str
    ledger_path: Path
    refusal_run_dir: Path
    process: subprocess.Popen[str] | None = None

    @property
    def job_dir(self) -> Path:
        return self.run_dir / self.job_name

    def _start(self, command: list[str]) -> subprocess.Popen[str]:
        self.process = subprocess.Popen(
            command,
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        return self.process

    def wait(self, timeout: float = 270) -> subprocess.CompletedProcess[str]:
        if self.process is None:
            raise AssertionError("no Harbor command has been started")
        command = self.process.args
        try:
            output, _ = self.process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            with suppress(ProcessLookupError):
                os.killpg(self.process.pid, signal.SIGKILL)
            output, _ = self.process.communicate()
            raise AssertionError(f"Harbor command timed out after {timeout}s: {output}") from exc
        return subprocess.CompletedProcess(command, self.process.returncode, output, "")

    def _run(self, command: list[str], timeout: float = 270) -> subprocess.CompletedProcess[str]:
        self._start(command)
        return self.wait(timeout)

    def run(self) -> subprocess.CompletedProcess[str]:
        return self._run(run_command(self.run_dir, self.job_name, self.proxy_url, "as_listed"))

    def start(self) -> subprocess.Popen[str]:
        return self._start(run_command(self.run_dir, self.job_name, self.proxy_url, "as_listed"))

    def resume(self) -> subprocess.CompletedProcess[str]:
        return self._run(resume_command(self.run_dir, self.job_name, self.proxy_url, "as_listed"))

    def run_refused(self) -> subprocess.CompletedProcess[str]:
        return self._run(
            machine_check_command(self.refusal_run_dir, self.job_name, "harbor-job-config.yaml")
        )

    def kill(self) -> None:
        if self.process is not None and self.process.poll() is None:
            try:
                os.killpg(self.process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            except PermissionError:
                self.process.terminate()


def _wait_for_health(process: subprocess.Popen[str], proxy_url: str) -> None:
    deadline = time.monotonic() + 10
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        code = process.poll()
        if code is not None:
            output, _ = process.communicate()
            raise AssertionError(f"proxy exited during startup with {code}: {output}")
        try:
            with urllib.request.urlopen(f"{proxy_url}/control/health", timeout=0.2) as response:
                if response.status == 200:
                    return
        except (OSError, urllib.error.URLError) as exc:
            last_error = exc
        time.sleep(0.05)
    raise AssertionError(f"proxy did not become healthy: {last_error}")


@cache
def _docker_proxy_network() -> tuple[str, str]:
    with socket.socket() as listener:
        listener.bind(("0.0.0.0", 0))
        listener.listen()
        port = listener.getsockname()[1]
        probe = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "--network",
                "bridge",
                "python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b",
                "python",
                "-c",
                "import socket, sys\n"
                "try:\n"
                "    socket.create_connection((sys.argv[1], int(sys.argv[2])), timeout=2).close()\n"
                "except OSError:\n"
                "    print('HOST_CONNECTION_FAILED')\n"
                "    raise SystemExit(1)\n",
                "host.docker.internal",
                str(port),
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    if probe.returncode == 0:
        return "127.0.0.1", "host.docker.internal"
    if "HOST_CONNECTION_FAILED" not in probe.stdout:
        raise AssertionError(
            f"Docker host-network probe failed ({probe.returncode}): {probe.stdout}{probe.stderr}"
        )
    gateway = subprocess.run(
        [
            "docker",
            "network",
            "inspect",
            "bridge",
            "--format",
            "{{(index .IPAM.Config 0).Gateway}}",
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if gateway.returncode != 0 or not gateway.stdout.strip():
        raise AssertionError(
            f"Docker bridge gateway lookup failed ({gateway.returncode}): "
            f"{gateway.stdout}{gateway.stderr}"
        )
    return "0.0.0.0", gateway.stdout.strip()


@pytest.fixture
def harbor_job(tmp_path: Path, request: pytest.FixtureRequest):
    options = getattr(request, "param", {})
    fake_behavior = options.get("fake", {})
    repetitions = options.get("repetitions", 1)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    refusal_run_dir = tmp_path / "refused"
    refusal_run_dir.mkdir()
    upstream = FakeUpstream(["ok"], model="fake-model")
    upstream_url = upstream.start()
    with socket.socket() as reserve:
        reserve.bind(("127.0.0.1", 0))
        port = reserve.getsockname()[1]
    proxy_host, container_host = _docker_proxy_network()
    proxy_url = f"http://127.0.0.1:{port}"
    ledger_path = run_dir / "proxy-ledger.jsonl"
    proxy_config_path = run_dir / "proxy-config.json"
    card = ModelCard(
        id="fake/model",
        upstream_model="fake-model",
        api="chat",
        endpoints=[f"{upstream_url}/v1/chat/completions"],
        key_env=[],
        tokens_per_minute=None,
        pricing={"input": 0.0, "output": 0.0, "cached_read": 0.0},
        variants={"default": {"temperature": 0.6}},
        withdrawn=False,
        reason=None,
        default_variant="default",
    )
    model_card = asdict(card)
    limits = Limits.from_dict({}, repetitions=repetitions, verifier_timeout_multiplier=1.0)
    write(
        proxy_config_path,
        ProxyConfig(
            "0.0.0.0",
            port,
            str(ledger_path),
            str(run_dir / "request-bodies"),
            model_card,
            limits.to_row(),
            0.0,
        ),
    )
    proxy_process = subprocess.Popen(
        [
            "uv",
            "run",
            "python",
            "-m",
            "harbor_research_runner.model_proxy",
            "--config",
            str(proxy_config_path),
        ],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    job_name = "harbor-job"
    job_file_path = tmp_path / "job.yaml"
    job_file_path.write_text(
        yaml.safe_dump(
            {
                "name": "contract-job",
                "model": card.id,
                "repetitions": repetitions,
                "concurrency": 1,
                "task_order": "as_listed",
                "tasks": {"path": str(TASK.parent)},
                "machine_check": "checks/unused.json",
                "arms": [
                    {
                        "label": "fake-arm",
                        "harness": "fake",
                        "commit": "0" * 40,
                    }
                ],
                "proxy": {
                    "host": proxy_host,
                    "port": port,
                    "container_host": container_host,
                },
                "limits": {},
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    job_definition = load_job(job_file_path)
    settings = agent_settings(
        arm_label="fake-arm",
        harness="fake",
        commit="fake",
        manifest_path="not-used-by-fake",
        container_host=container_host,
        proxy_port=port,
        variant=None,
        model_card=model_card,
        fake=fake_behavior,
    )
    config = harbor_job_config(
        job_definition,
        [TASK.name],
        [("harbor_research_runner.harness.fake:FakeHarnessAgent", settings)],
        verifier_timeout_multiplier=1.0,
    )
    write_job_config(run_dir / "harbor-job-config.yaml", config)
    write_job_config(refusal_run_dir / "harbor-job-config.yaml", config)
    job = HarborJob(run_dir, job_name, proxy_url, ledger_path, refusal_run_dir)
    try:
        _wait_for_health(proxy_process, proxy_url)
        if not options.get("defer_run", False):
            initial = job.run()
            assert initial.returncode == 0, initial.stdout
        yield job
    finally:
        job.kill()
        if proxy_process.poll() is None:
            proxy_process.send_signal(signal.SIGTERM)
        try:
            proxy_process.wait(timeout=12)
        except subprocess.TimeoutExpired:
            proxy_process.kill()
            proxy_process.wait(timeout=2)
        upstream.stop()


@dataclass
class E2ERun:
    root: Path
    runs_dir: Path
    evidence_dir: Path
    job_path: Path
    manifest_path: Path
    machine_check_path: Path
    job_data: dict
    upstream: FakeUpstream
    proxy_port: int
    process: subprocess.Popen[str] | None = None
    last_result: subprocess.CompletedProcess[str] | None = None

    @property
    def run_dir(self) -> Path:
        paths = sorted(
            path
            for path in self.runs_dir.iterdir()
            if path.is_dir() and (path / "run-metadata.json").is_file()
        )
        if len(paths) != 1:
            raise AssertionError(f"expected one run folder, found {paths}")
        return paths[0]

    @property
    def run_id(self) -> str:
        return self.run_dir.name

    @property
    def events(self) -> list[dict]:
        path = self.run_dir / "run-events.jsonl"
        return read_rows(path).rows if path.is_file() else []

    @property
    def ledger(self) -> list[dict]:
        path = self.run_dir / "proxy-ledger.jsonl"
        return read_rows(path).rows if path.is_file() else []

    def cli(self, arguments: list[str], timeout: float = 270) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["uv", "run", "hrr", *arguments],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )

    def start(
        self,
        *,
        job_path: Path | None = None,
        runs_dir: Path | None = None,
        resume: bool = False,
        stop_launching_at: str | None = None,
    ) -> subprocess.Popen[str]:
        if self.process is not None and self.process.poll() is None:
            raise AssertionError("a runner is already active")
        selected_runs_dir = runs_dir or self.runs_dir
        arguments = ["run"]
        if resume:
            arguments.extend(["--resume", self.run_id])
        else:
            arguments.append(str(job_path or self.job_path))
        arguments.extend(
            [
                "--runs-dir",
                str(selected_runs_dir),
                "--evidence-dir",
                str(self.evidence_dir),
                "--manifest",
                str(self.manifest_path),
            ]
        )
        if stop_launching_at is not None:
            arguments.extend(["--stop-launching-at", stop_launching_at])
        self.process = subprocess.Popen(
            ["uv", "run", "hrr", *arguments],
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        return self.process

    def wait(self, timeout: float = 270) -> subprocess.CompletedProcess[str]:
        if self.process is None:
            raise AssertionError("no runner has been started")
        command = self.process.args
        try:
            output, _ = self.process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            self.stop_process_groups()
            output, _ = self.process.communicate()
            raise AssertionError(f"hrr timed out after {timeout}s: {output}") from exc
        self.last_result = subprocess.CompletedProcess(command, self.process.returncode, output, "")
        return self.last_result

    def run(self) -> subprocess.CompletedProcess[str]:
        self.start()
        return self.wait()

    def resume(self) -> subprocess.CompletedProcess[str]:
        self.kill_proxy()
        _clean_docker_and_wait(self.runs_dir)
        self.start(resume=True)
        return self.wait()

    def check_machine(self) -> subprocess.CompletedProcess[str]:
        return self.cli(
            [
                "check-machine",
                "--job",
                str(self.job_path),
                "--out",
                str(self.machine_check_path),
                "--runs-dir",
                str(self.runs_dir),
            ]
        )

    def events_if_ready(self) -> list[dict]:
        path = self._run_path_if_ready()
        return read_rows(path / "run-events.jsonl").rows if path is not None else []

    def ledger_if_ready(self) -> list[dict]:
        path = self._run_path_if_ready()
        return read_rows(path / "proxy-ledger.jsonl").rows if path is not None else []

    def _run_path_if_ready(self) -> Path | None:
        paths = sorted(
            path
            for path in self.runs_dir.iterdir()
            if path.is_dir() and (path / "run-metadata.json").is_file()
        )
        return paths[0] if len(paths) == 1 else None

    def wait_for_run(self, timeout: float = 30) -> Path:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            path = self._run_path_if_ready()
            if path is not None:
                return path
            if self.process is not None and self.process.poll() is not None:
                output, _ = self.process.communicate()
                raise AssertionError(f"hrr exited before creating a run folder: {output}")
            time.sleep(0.05)
        raise AssertionError("hrr did not create a run folder")

    def wait_for_main_trial(self, timeout: float = 60) -> str:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            run_path = self._run_path_if_ready()
            if run_path is not None:
                for row in self.ledger_if_ready():
                    if row.get("type") != "open":
                        continue
                    trial_dir = run_path / "harbor-job" / row["trial"]
                    if trial_dir.is_dir() and not (trial_dir / "result.json").exists():
                        return row["trial"]
            if self.process is not None and self.process.poll() is not None:
                output, _ = self.process.communicate()
                raise AssertionError(f"hrr exited before a main trial opened: {output}")
            time.sleep(0.05)
        raise AssertionError("no unfinished main trial opened")

    def _process_groups(self) -> dict[int, set[str]]:
        result = subprocess.run(
            ps_command(),
            capture_output=True,
            text=True,
            check=True,
        )
        groups: dict[int, set[str]] = {}
        for line in result.stdout.splitlines():
            fields = line.split(None, 2)
            if len(fields) != 3 or str(self.runs_dir) not in fields[2]:
                continue
            groups.setdefault(int(fields[1]), set()).add(fields[2])
        return groups

    def _signal_groups(self, groups: set[int]) -> None:
        for pgid in groups:
            if pgid == os.getpgrp():
                continue
            with suppress(ProcessLookupError):
                os.killpg(pgid, signal.SIGKILL)

    def kill_runner(self) -> None:
        groups = self._process_groups()
        groups_to_kill = {
            pgid
            for pgid, commands in groups.items()
            if any(
                "hrr run" in command or "harbor run" in command or "harbor jobs resume" in command
                for command in commands
            )
        }
        killed = time.monotonic()
        self._signal_groups(groups_to_kill)
        if self.process is not None and self.process.poll() is None:
            self.process.kill()
            with suppress(subprocess.TimeoutExpired):
                self.process.wait(timeout=10)
        self._wait_for_run_processes(killed, RUN_PROCESSES_STOP_WITHIN_S)

    def _wait_for_run_processes(self, killed: float, stop_within_s: float) -> None:
        deadline = killed + stop_within_s
        while (live := pids_naming_run(self.run_dir)) and time.monotonic() < deadline:
            time.sleep(0.2)
        if live:
            raise AssertionError(
                f"processes still name the run folder {stop_within_s}s after the runner died: {live}"
            )

    def kill_runner_only(self, stop_within_s: float) -> float:
        runner_pids = [
            row["pid"]
            for row in self.events
            if row.get("type") in {"launch", "resume"} and isinstance(row.get("pid"), int)
        ]
        if not runner_pids:
            raise AssertionError("no runner pid was recorded")
        killed = time.monotonic()
        os.kill(runner_pids[-1], signal.SIGKILL)
        self._wait_for_run_processes(killed, stop_within_s)
        stopped_after = time.monotonic() - killed
        if self.process is not None:
            try:
                output, _ = self.process.communicate(timeout=10)
            except subprocess.TimeoutExpired as exc:
                raise AssertionError("the hrr launcher did not exit after its runner died") from exc
            self.last_result = subprocess.CompletedProcess(
                self.process.args, self.process.returncode, output, ""
            )
        return stopped_after

    def kill_proxy(self) -> None:
        groups = self._process_groups()
        proxy_groups = {
            pgid
            for pgid, commands in groups.items()
            if any("harbor_research_runner.model_proxy" in command for command in commands)
        }
        self._signal_groups(proxy_groups)

    def stop_process_groups(self) -> None:
        deadline = time.monotonic() + 10
        while True:
            groups = self._process_groups()
            if not groups:
                break
            self._signal_groups(set(groups))
            if time.monotonic() >= deadline:
                break
            time.sleep(0.05)
        if self.process is not None and self.process.poll() is None:
            self.process.kill()
            with suppress(subprocess.TimeoutExpired):
                self.process.wait(timeout=5)
        remaining = self._process_groups()
        assert not remaining, f"process groups remained after teardown: {remaining}"

    def second_job(self) -> tuple[Path, Path]:
        second_runs_dir = self.root / "second-runs"
        second_runs_dir.mkdir()
        second_path = self.root / "second-job.yaml"
        data = dict(self.job_data)
        data["name"] = "lifecycle-job-second"
        data["proxy"] = dict(data["proxy"])
        data["proxy"]["port"] = _free_port()
        _write_job_file(second_path, data)
        return second_path, second_runs_dir

    def clean_docker(self, apply: bool) -> subprocess.CompletedProcess[str]:
        return self.cli(
            [
                "clean-docker",
                *(["--apply"] if apply else []),
                "--runs-dir",
                str(self.runs_dir),
            ]
        )


def _free_port() -> int:
    with socket.socket() as reserve:
        reserve.bind(("127.0.0.1", 0))
        return int(reserve.getsockname()[1])


def _write_job_file(path: Path, data: dict) -> None:
    path.write_text(yaml.safe_dump(data, sort_keys=True), encoding="utf-8")
    load_job(path)


def _docker_listing() -> str:
    result = subprocess.run(["docker", "ps", "-a"], capture_output=True, text=True, check=False)
    return result.stdout + result.stderr


def _clean_docker_and_wait(runs_dir: Path) -> None:
    runs_dir.mkdir(parents=True, exist_ok=True)
    command = ["uv", "run", "hrr", "clean-docker", "--apply", "--runs-dir", str(runs_dir)]
    result = subprocess.run(
        command, cwd=ROOT, capture_output=True, text=True, timeout=60, check=False
    )
    if result.returncode != 0:
        raise AssertionError(
            f"clean-docker --apply failed ({result.returncode}): {result.stdout}{result.stderr}\n"
            f"docker ps -a:\n{_docker_listing()}"
        )
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        dry_run = subprocess.run(
            ["uv", "run", "hrr", "clean-docker", "--runs-dir", str(runs_dir)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if dry_run.returncode == 0:
            return
        time.sleep(0.1)
    raise AssertionError(
        f"Docker residue remained after cleanup: {dry_run.stdout}{dry_run.stderr}\n"
        f"docker ps -a:\n{_docker_listing()}"
    )


@pytest.fixture
def e2e_run(tmp_path: Path, request: pytest.FixtureRequest):
    options = getattr(request, "param", {})
    if shutil.which("docker") is None:
        pytest.skip("docker not installed")
    docker_info = subprocess.run(["docker", "info"], capture_output=True, text=True, check=False)
    if docker_info.returncode != 0:
        pytest.skip(f"docker daemon is unavailable: {docker_info.stderr.strip()}")

    runs_dir = tmp_path / "runs"
    evidence_dir = tmp_path / "evidence"
    _clean_docker_and_wait(runs_dir)
    proxy_host, container_host = _docker_proxy_network()

    upstream = FakeUpstream(options.get("upstream_script", ["ok"]), model="fake-model")
    upstream_url = upstream.start()
    proxy_port = _free_port()
    card = ModelCard(
        id="e2e/fake-lifecycle",
        upstream_model="fake-model",
        api="chat",
        endpoints=[f"{upstream_url}/v1/chat/completions"],
        key_env=[],
        tokens_per_minute=None,
        pricing={"input": 0.0, "output": 0.0, "cached_read": 0.0},
        variants={"default": {"temperature": 0.6}},
        withdrawn=False,
        reason=None,
        default_variant="default",
    )
    card_path = tmp_path / "e2e" / "fake-lifecycle.json"
    card_path.parent.mkdir(parents=True)
    card_data = asdict(card)
    card_data.pop("request", None)
    card_path.write_text(json.dumps(card_data, sort_keys=True), encoding="utf-8")
    manifest_path = tmp_path / "harness-manifest.toml"
    manifest_path.write_text("", encoding="utf-8")

    arm_count = options.get("arms", 1)
    repetitions = options.get("repetitions", 1)
    labels = [f"arm-{index + 1}" for index in range(arm_count)]
    fake_behavior = options.get("fake", {})
    machine_check_path = tmp_path / "machine-check.json"
    study_tasks_dir = tmp_path / "tasks"
    study_task = study_tasks_dir / "say-hello"
    shutil.copytree(TASK, study_task)
    task_toml = study_task / "task.toml"
    task_text = task_toml.read_text(encoding="utf-8")
    task_table_start = task_text.index("[task]\n")
    metadata_table_start = task_text.index("[metadata]\n", task_table_start)
    task_toml.write_text(
        task_text[:task_table_start] + task_text[metadata_table_start:], encoding="utf-8"
    )
    if "agent_timeout_s" in options:
        task_toml.write_text(
            task_toml.read_text(encoding="utf-8").replace(
                "timeout_sec = 120.0", f"timeout_sec = {options['agent_timeout_s']}", 1
            ),
            encoding="utf-8",
        )
    limits = {
        "min_spare_memory_mb": 0,
        "max_swapped_out_pages": 100000,
        "min_free_disk_gb": 0,
        "host_breach_halt_s": 30,
        "inactivity_margin_s": 30,
        "launch_min_interval_s": 0,
    }
    limits.update(options.get("limits", {}))
    job_data = {
        "name": "lifecycle-job",
        "model": card.id,
        "repetitions": repetitions,
        "concurrency": 1,
        "task_order": "as_listed",
        "tasks": {
            "path": str(study_tasks_dir),
            "include": [study_task.name],
            "exclude": [],
        },
        "machine_check": str(machine_check_path),
        "arms": [
            {
                "label": label,
                "harness": "fake",
                "commit": "0" * 40,
                "fake": fake_behavior,
            }
            for label in labels
        ],
        "proxy": {
            "host": proxy_host,
            "port": proxy_port,
            "container_host": container_host,
        },
        "limits": limits,
    }
    job_path = tmp_path / "job.yaml"
    _write_job_file(job_path, job_data)

    if options.get("write_machine_check", True):
        write_machine_check(
            machine_check_path,
            MachineCheck(
                created_t=time.time(),
                tool_version=version("harbor-research-runner"),
                harbor_version=version("harbor"),
                host_fingerprint=host_fingerprint(),
                task_source={"path": str(study_tasks_dir)},
                concurrency=1,
                tasks=[
                    {
                        "name": study_task.name,
                        "digest": "0" * 64,
                        "memory_mb": 512,
                        "oracle_reward": 1.0,
                        "agent_s": 1.0,
                        "agent_budget_s": 120.0,
                        "verifier_s": 1.0,
                        "verifier_budget_s": 60.0,
                        "nothing_reward": None,
                    }
                ],
                included=[study_task.name],
                verifier_timeout_multiplier=1.0,
                host_profile=host_profile(),
            ),
        )

    test_run = E2ERun(
        tmp_path,
        runs_dir,
        evidence_dir,
        job_path,
        manifest_path,
        machine_check_path,
        job_data,
        upstream,
        proxy_port,
    )
    try:
        yield test_run
    finally:
        try:
            test_run.stop_process_groups()
        finally:
            try:
                upstream.stop()
            finally:
                _clean_docker_and_wait(runs_dir)
