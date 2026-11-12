from __future__ import annotations

import signal
import socket
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path

import pytest
import yaml

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
from harbor_research_runner.records.limits import Limits
from harbor_research_runner.records.proxy_control import ProxyConfig, write
from tests.support.fake_upstream import FakeUpstream

ROOT = Path(__file__).resolve().parents[2]
TASK = ROOT / "src/harbor_research_runner/run/canary_task/say-hello"


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

    def _run(self, command: list[str], timeout: float = 270) -> subprocess.CompletedProcess[str]:
        self.process = subprocess.Popen(
            command,
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            output, _ = self.process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            self.process.kill()
            output, _ = self.process.communicate()
            raise AssertionError(f"Harbor command timed out after {timeout}s: {output}") from exc
        return subprocess.CompletedProcess(command, self.process.returncode, output, "")

    def run(self) -> subprocess.CompletedProcess[str]:
        return self._run(run_command(self.run_dir, self.job_name, self.proxy_url, "as_listed"))

    def resume(self) -> subprocess.CompletedProcess[str]:
        return self._run(resume_command(self.run_dir, self.job_name, self.proxy_url, "as_listed"))

    def run_refused(self) -> subprocess.CompletedProcess[str]:
        return self._run(
            machine_check_command(self.refusal_run_dir, self.job_name, "harbor-job-config.yaml")
        )

    def kill(self) -> None:
        if self.process is not None and self.process.poll() is None:
            self.process.send_signal(signal.SIGTERM)


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
                    "host": "127.0.0.1",
                    "port": port,
                    "container_host": "host.docker.internal",
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
        container_host="host.docker.internal",
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
