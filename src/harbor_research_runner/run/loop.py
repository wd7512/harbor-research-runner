from __future__ import annotations

import hashlib
import os
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from harbor_research_runner.job_config.job_file import Arm, Job, TaskSource
from harbor_research_runner.job_config.model_cards import ModelCard
from harbor_research_runner.records import jsonl, run_metadata
from harbor_research_runner.records.limits import Limits
from harbor_research_runner.records.machine_check import MachineCheck
from harbor_research_runner.records.proxy_control import ProxyConfig
from harbor_research_runner.records.proxy_control import write as write_proxy_config
from harbor_research_runner.records.run_events import Launch, LaunchCheck, Resume
from harbor_research_runner.run import process_group
from harbor_research_runner.run.checks import CheckResult
from harbor_research_runner.run.tool_identity import ToolIdentity


class RefusedToStart(Exception):
    pass


class RunSession:
    def __init__(
        self,
        run_dir: Path,
        job: Job,
        card: ModelCard,
        limits: Limits,
        metadata: run_metadata.RunMetadata,
        environment: Mapping[str, str],
        tool: ToolIdentity,
        manifest_path: Path | None = None,
    ) -> None:
        self.run_dir = run_dir
        self.job = job
        self.card = card
        self.limits = limits
        self.metadata = metadata
        self.environment = dict(environment)
        self.tool = tool
        self.manifest_path = manifest_path
        self._sampler: subprocess.Popen | None = None
        self._proxy: subprocess.Popen | None = None

    @classmethod
    def create(
        cls,
        job: Job,
        card: ModelCard,
        manifest_path: Path,
        runs_dir: Path,
        environment: Mapping[str, str],
        check_results: list[CheckResult],
        tasks: dict,
        arms: list[dict],
        machine_check: MachineCheck,
        now,
        tool: ToolIdentity,
        host: dict,
    ) -> RunSession:
        expected_host_fields = {"fingerprint", "arch", "cpus", "memory_mb", "docker_version"}
        if set(host) != expected_host_fields:
            missing = sorted(expected_host_fields - set(host))
            extra = sorted(set(host) - expected_host_fields)
            raise ValueError(f"host fields differ: missing={missing}, extra={extra}")
        created_t = now()
        timestamp = datetime.fromtimestamp(created_t, UTC).strftime("%Y%m%dT%H%M%S%fZ")
        run_id = f"{job.name}-{timestamp}"
        run_dir = runs_dir / run_id
        run_dir.mkdir(parents=True, exist_ok=False)
        limits = Limits.from_dict(
            job.limit_overrides, job.repetitions, machine_check.verifier_timeout_multiplier
        )
        _append_event(run_dir, Launch(created_t, tool.version, tool.commit, os.getpid()))
        for result in check_results:
            _append_event(run_dir, LaunchCheck(now(), result.name, result.ok, result.detail))
        if any(not result.ok for result in check_results):
            raise RefusedToStart("one or more launch checks failed")
        metadata = run_metadata.RunMetadata(
            run_id=run_id,
            created_t=created_t,
            tool_version=tool.version,
            tool_commit=tool.commit,
            harbor_version=machine_check.harbor_version,
            job=_job_to_row(job),
            job_file_sha256=hashlib.sha256(job.path.read_bytes()).hexdigest(),
            model_card=_card_to_row(card),
            arms=arms,
            task_set=tasks,
            machine_check_sha256=hashlib.sha256(job.machine_check.read_bytes()).hexdigest(),
            host=dict(host),
            proxy={"host": job.proxy_host, "port": job.proxy_port},
            limits=limits.to_row(),
        )
        run_metadata.write_once(run_dir / "run-metadata.json", metadata)
        return cls(run_dir, job, card, limits, metadata, environment, tool, manifest_path)

    @classmethod
    def load(cls, run_dir: Path, environment: Mapping[str, str]) -> RunSession:
        metadata = run_metadata.read(run_dir / "run-metadata.json")
        job = _job_from_row(metadata.job)
        card = ModelCard(**metadata.model_card)
        limits = Limits.from_row(metadata.limits)
        return cls(
            run_dir,
            job,
            card,
            limits,
            metadata,
            environment,
            ToolIdentity(metadata.tool_version, metadata.tool_commit),
        )

    def mark_resumed(self, now) -> None:
        self.event(Resume(now(), self.tool.version, self.tool.commit, os.getpid()))

    def event(self, event) -> None:
        _append_event(self.run_dir, event)

    def start_services(self, restart: bool) -> None:
        config = ProxyConfig(
            host=self.job.proxy_host,
            port=self.job.proxy_port,
            ledger_path=str(self.run_dir / "proxy-ledger.jsonl"),
            bodies_dir=str(self.run_dir / "request-bodies"),
            model=_card_to_row(self.card),
            limits=self.limits.to_row(),
            restart_cooldown_s=60.0 if restart else 0.0,
        )
        config_path = self.run_dir / "proxy-config.json"
        write_proxy_config(config_path, config)
        environment = os.environ.copy()
        environment.update(
            {name: value for name, value in self.environment.items() if name in self.card.key_env}
        )
        try:
            self._sampler = process_group.spawn_group(
                [
                    "uv",
                    "run",
                    "python",
                    "-m",
                    "harbor_research_runner.docker_host.sampler",
                    "--out",
                    str(self.run_dir / "resource-stats.jsonl"),
                ],
                env=environment,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.STDOUT,
            )
            self._proxy = process_group.spawn_group(
                [
                    "uv",
                    "run",
                    "python",
                    "-m",
                    "harbor_research_runner.model_proxy",
                    "--config",
                    str(config_path),
                ],
                env=environment,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.STDOUT,
            )
        except BaseException:
            self.stop_services()
            raise
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if not self.proxy_alive():
                self.stop_services()
                raise RuntimeError("proxy exited before becoming healthy")
            try:
                with urllib.request.urlopen(
                    f"http://{self.job.proxy_host}:{self.job.proxy_port}/control/health",
                    timeout=0.2,
                ) as response:
                    if response.status == 200:
                        return
            except (OSError, urllib.error.URLError):
                pass
            time.sleep(0.5)
        self.stop_services()
        raise TimeoutError("proxy did not become healthy within 30 seconds")

    def stop_services(self) -> None:
        for name in ("_proxy", "_sampler"):
            process = getattr(self, name)
            if process is not None and process.poll() is None:
                process_group.stop_group(process, grace_s=15)
            setattr(self, name, None)

    def proxy_alive(self) -> bool:
        return self._proxy is not None and self._proxy.poll() is None


def _job_to_row(job: Job) -> dict:
    row = asdict(job)
    row["path"] = str(job.path)
    row["machine_check"] = str(job.machine_check)
    return row


def _append_event(run_dir: Path, event) -> None:
    fd = jsonl.open_for_append(run_dir / "run-events.jsonl")
    try:
        jsonl.append_row(fd, event.to_row())
    finally:
        os.close(fd)


def _job_from_row(row: dict) -> Job:
    values = dict(row)
    task_values = dict(values["tasks"])
    task_values["include"] = tuple(task_values["include"])
    task_values["exclude"] = tuple(task_values["exclude"])
    values["tasks"] = TaskSource(**task_values)
    values["arms"] = tuple(Arm(**arm) for arm in values["arms"])
    values["path"] = Path(values["path"])
    values["machine_check"] = Path(values["machine_check"])
    return Job(**values)


def _card_to_row(card: ModelCard) -> dict:
    return asdict(card)
