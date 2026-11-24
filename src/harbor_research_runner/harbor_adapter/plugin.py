from __future__ import annotations

import asyncio
import logging
import math
import time
import tomllib
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

from harbor.environments.docker.docker import DockerEnvironment
from harbor.job import Job
from harbor.models.job.plugin import BaseJobPlugin
from harbor.models.job.result import JobResult
from harbor.models.trial.config import TrialConfig
from harbor.trial.hooks import TrialEvent, TrialHookEvent

from harbor_research_runner.docker_host.cleanup import docker_memory_mb
from harbor_research_runner.harbor_adapter.job_folder import HarborFolderError, task_timeouts
from harbor_research_runner.records.limits import Limits, effective_memory_mb
from harbor_research_runner.records.proxy_control import (
    CloseMessage,
    OpenMessage,
)
from harbor_research_runner.records.proxy_control import (
    read as read_proxy_config,
)

logger = logging.getLogger(__name__)

_TASK_TOML = "task.toml"
_INITIAL_BACKOFF_S = 1.0
_MAX_BACKOFF_S = 60.0


class RunDockerEnvironment(DockerEnvironment):
    def __init__(self, *, task_env_config, memory_multiplier: float = 1.0, **kwargs):
        if (
            isinstance(memory_multiplier, bool)
            or not isinstance(memory_multiplier, (int, float))
            or not math.isfinite(memory_multiplier)
            or memory_multiplier <= 0
        ):
            raise ValueError("memory_multiplier must be a finite positive number")
        task_env_config = task_env_config.model_copy(deep=True)
        declared_memory = task_env_config.memory_mb
        if declared_memory is not None:
            kwargs["override_memory_mb"] = effective_memory_mb(declared_memory, memory_multiplier)
        super().__init__(task_env_config=task_env_config, **kwargs)


class MemoryReservationGate:
    def __init__(
        self,
        *,
        memory_reserve_mb: int,
        memory_multiplier: float = 1.0,
        launch_min_interval_s: float = 0.0,
        docker_memory_reader: Callable[[], int] = docker_memory_mb,
        sleep: Callable[[float], Any] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
        initial_backoff_s: float = _INITIAL_BACKOFF_S,
        max_backoff_s: float = _MAX_BACKOFF_S,
    ) -> None:
        if isinstance(memory_reserve_mb, bool) or not isinstance(memory_reserve_mb, int):
            raise ValueError("memory_reserve_mb must be an integer")
        if memory_reserve_mb < 0:
            raise ValueError("memory_reserve_mb must be non-negative")
        if (
            isinstance(launch_min_interval_s, bool)
            or not isinstance(launch_min_interval_s, (int, float))
            or not math.isfinite(launch_min_interval_s)
            or launch_min_interval_s < 0
        ):
            raise ValueError(
                "launch_min_interval_s must be a finite non-negative number; "
                f"got {launch_min_interval_s!r}"
            )
        if initial_backoff_s <= 0 or max_backoff_s < initial_backoff_s:
            raise ValueError("backoff bounds must satisfy 0 < initial <= max")
        self.memory_reserve_mb = memory_reserve_mb
        self.memory_multiplier = memory_multiplier
        self.launch_min_interval_s = float(launch_min_interval_s)
        self._docker_memory_reader = docker_memory_reader
        self._sleep = sleep
        self._clock = clock
        self._initial_backoff_s = initial_backoff_s
        self._max_backoff_s = max_backoff_s
        self._last_launch: float | None = None
        self._reservations: dict[str, int] = {}

    @property
    def reserved_memory_mb(self) -> int:
        return sum(self._reservations.values())

    async def wait_for_headroom(self, *, trial: str, task: str, memory_mb: int) -> None:
        if isinstance(memory_mb, bool) or not isinstance(memory_mb, int) or memory_mb < 0:
            raise ValueError(f"task {task!r} has invalid memory_mb {memory_mb!r}")
        backoff = self._initial_backoff_s
        attempts = 0
        while True:
            try:
                docker_memory = int(await asyncio.to_thread(self._docker_memory_reader))
            except (OSError, TypeError, ValueError) as exc:
                raise RuntimeError(str(exc)) from exc
            if memory_mb + self.memory_reserve_mb > docker_memory:
                raise RuntimeError(
                    f"task {task!r} reservation exceeds Docker memory: "
                    f"reserved_mb={self.reserved_memory_mb}, task_memory_mb={memory_mb}, "
                    f"memory_reserve_mb={self.memory_reserve_mb}, docker_memory_mb={docker_memory}"
                )
            now = self._clock()
            required_memory = self.reserved_memory_mb + memory_mb + self.memory_reserve_mb
            enough_memory = required_memory <= docker_memory
            since_last = None if self._last_launch is None else now - self._last_launch
            interval_passed = since_last is None or since_last >= self.launch_min_interval_s
            if enough_memory and interval_passed:
                if attempts:
                    logger.info("launch gate: admitting %s after %d deferral(s)", trial, attempts)
                self._reservations[trial] = memory_mb
                self._last_launch = now
                return
            attempts += 1
            if attempts == 1:
                if not enough_memory:
                    logger.warning(
                        "launch gate: deferring %s (task %s): reserved %d MB + task %d MB "
                        "+ reserve %d MB > Docker memory %d MB",
                        trial,
                        task,
                        self.reserved_memory_mb,
                        memory_mb,
                        self.memory_reserve_mb,
                        docker_memory,
                    )
                else:
                    logger.warning(
                        "launch gate: pacing %s (%.1fs since last launch)", trial, since_last
                    )
            else:
                logger.debug("launch gate: still deferring %s (attempt %d)", trial, attempts)
            await self._sleep(backoff)
            backoff = min(self._max_backoff_s, backoff * 2)

    def release(self, trial: str) -> None:
        self._reservations.pop(trial, None)


class StopLaunchingGate:
    def __init__(
        self,
        stop_launching_at: float | None,
        *,
        sleep: Callable[[float], Any] = asyncio.sleep,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.stop_launching_at = stop_launching_at
        self._sleep = sleep
        self._clock = clock

    async def wait_until_open(self, *, trial: str, task: str) -> None:
        if self.stop_launching_at is None:
            return
        backoff = _INITIAL_BACKOFF_S
        attempts = 0
        while self._clock() >= self.stop_launching_at:
            attempts += 1
            if attempts == 1:
                logger.warning(
                    "launch deadline: deferring %s (task %s) until the next run pass",
                    trial,
                    task,
                )
            await self._sleep(backoff)
            backoff = min(_MAX_BACKOFF_S, backoff * 2)


def _post(url: str, body: bytes) -> int:
    request = Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
    with urlopen(request, timeout=10) as response:
        return response.status


def sort_slowest_first(
    configs: Sequence[TrialConfig], timeout_sec: Callable[[TrialConfig], float]
) -> list[TrialConfig]:
    return sorted(configs, key=lambda config: -timeout_sec(config))


class RunPlugin(BaseJobPlugin):
    def __init__(
        self,
        proxy_url: str,
        limits_file: str | Path,
        task_order: str,
        stop_launching_at: str | None = None,
    ) -> None:
        super().__init__(
            proxy_url=proxy_url,
            limits_file=limits_file,
            task_order=task_order,
            stop_launching_at=stop_launching_at,
        )
        if task_order not in {"as_listed", "slowest_first"}:
            raise ValueError(f"task_order must be as_listed or slowest_first; got {task_order!r}")
        proxy_config = read_proxy_config(Path(limits_file))
        limits = Limits.from_row(proxy_config.limits)
        self.proxy_url = proxy_url.rstrip("/")
        self.task_order = task_order
        self._gate = MemoryReservationGate(
            memory_reserve_mb=limits.memory_reserve_mb,
            launch_min_interval_s=limits.launch_min_interval_s,
            memory_multiplier=limits.memory_kill_multiplier,
        )
        self._job: Job | None = None
        deadline = None
        if stop_launching_at is not None:
            parsed_deadline = datetime.fromisoformat(stop_launching_at)
            if parsed_deadline.tzinfo is None or parsed_deadline.utcoffset() is None:
                raise ValueError("stop_launching_at must include a timezone")
            deadline = parsed_deadline.timestamp()
        self._stop_launching_gate = StopLaunchingGate(deadline)

    @staticmethod
    def _task_label(config: TrialConfig) -> str:
        if config.task.path is not None:
            return config.task.path.name
        if config.task.name is not None:
            return config.task.name
        return config.trial_name or "<unknown task>"

    @staticmethod
    def _task_dir(job: Job, config: TrialConfig) -> Path | None:
        task = config.task
        if task.path is not None and not task.is_git_task():
            return task.path
        try:
            task_id = task.get_task_id()
            downloaded = job._task_download_results.get(task_id)
        except (AttributeError, TypeError, ValueError):
            return None
        return downloaded.path if downloaded is not None else None

    def _timeout_sec(self, job: Job, config: TrialConfig) -> float:
        label = self._task_label(config)
        task_dir = self._task_dir(job, config)
        if task_dir is None:
            raise HarborFolderError(f"cannot resolve task folder for {label!r}")
        path = task_dir / _TASK_TOML
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise HarborFolderError(f"cannot read task.toml for {label!r}: {exc}") from exc
        agent_timeout, _ = task_timeouts(label, text)
        if not math.isfinite(agent_timeout) or agent_timeout <= 0:
            raise HarborFolderError(
                f"task {label!r} has invalid agent timeout_sec {agent_timeout!r}"
            )
        return agent_timeout

    def _task_memory_mb(self, config: TrialConfig) -> int:
        label = self._task_label(config)
        task_dir = self._task_dir(self._job, config) if self._job is not None else None
        if task_dir is None:
            raise HarborFolderError(f"cannot resolve task folder for {label!r}")
        path = task_dir / _TASK_TOML
        try:
            task_data = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise HarborFolderError(f"cannot read task memory for {label!r}: {exc}") from exc
        environment = task_data.get("environment", {})
        memory_mb = environment.get("memory_mb") if isinstance(environment, dict) else None
        if isinstance(memory_mb, bool) or not isinstance(memory_mb, int) or memory_mb < 0:
            raise HarborFolderError(
                f"task {label!r} has invalid environment.memory_mb {memory_mb!r}"
            )
        return memory_mb

    async def _on_trial_start(self, event: TrialHookEvent) -> None:
        memory_mb = self._task_memory_mb(event.config)
        await self._gate.wait_for_headroom(
            trial=event.trial_name,
            task=event.task_name,
            memory_mb=effective_memory_mb(memory_mb, self._gate.memory_multiplier) or 0,
        )

    async def _on_agent_start(self, event: TrialHookEvent) -> None:
        await self._stop_launching_gate.wait_until_open(
            trial=event.trial_name, task=event.task_name
        )
        kwargs = event.config.agent.kwargs
        arm = kwargs.get("arm_label") if isinstance(kwargs, dict) else None
        if not isinstance(arm, str) or not arm:
            raise ValueError("agent arm_label must be a non-empty string")
        body = OpenMessage(event.trial_name, event.task_name, arm).to_json().encode("utf-8")
        status = await asyncio.to_thread(_post, f"{self.proxy_url}/control/open", body)
        if status != 200:
            raise RuntimeError(f"proxy open returned HTTP {status}")

    async def _on_agent_end(self, event: TrialHookEvent) -> None:
        kwargs = event.config.agent.kwargs
        arm = kwargs.get("arm_label") if isinstance(kwargs, dict) else None
        if arm is None:
            return
        body = CloseMessage(event.trial_name).to_json().encode("utf-8")
        for attempt in range(3):
            try:
                status = await asyncio.to_thread(_post, f"{self.proxy_url}/control/close", body)
                if status != 200:
                    raise RuntimeError(f"proxy close returned HTTP {status}")
                return
            except Exception as exc:
                if attempt == 2:
                    logger.error(
                        "could not close proxy trial %s after 3 attempts: %s", event.trial_name, exc
                    )
                    return
                await asyncio.sleep(1.0)

    async def _on_trial_end(self, event: TrialHookEvent) -> None:
        self._gate.release(event.trial_name)

    async def on_job_start(self, job: Job) -> None:
        self._job = job
        if self.task_order == "slowest_first":
            job._remaining_trial_configs = sort_slowest_first(
                job._remaining_trial_configs,
                lambda config: self._timeout_sec(job, config),
            )
        job.add_hook(TrialEvent.START, self._on_trial_start)
        job.add_hook(TrialEvent.AGENT_START, self._on_agent_start)
        job.add_hook(TrialEvent.AGENT_END, self._on_agent_end)
        job.add_hook(TrialEvent.END, self._on_trial_end)

    async def on_job_end(self, job_result: JobResult) -> None:
        del job_result
