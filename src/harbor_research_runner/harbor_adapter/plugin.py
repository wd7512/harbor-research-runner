from __future__ import annotations

import asyncio
import logging
import math
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

from harbor.job import Job
from harbor.models.job.plugin import BaseJobPlugin
from harbor.models.job.result import JobResult
from harbor.models.trial.config import TrialConfig
from harbor.trial.hooks import TrialEvent, TrialHookEvent

from harbor_research_runner.docker_host.resources import spare_memory_mb
from harbor_research_runner.harbor_adapter.job_folder import HarborFolderError, task_timeouts
from harbor_research_runner.records.limits import Limits
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


def spare_is_sufficient(spare_mb: int, required_mb: int) -> bool:
    """Return whether measured spare memory meets the configured limit."""
    return spare_mb >= required_mb


class RamSpareGate:
    def __init__(
        self,
        *,
        min_spare_memory_mb: int,
        launch_min_interval_s: float = 0.0,
        spare_reader: Callable[[], int] = spare_memory_mb,
        sleep: Callable[[float], Any] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
        initial_backoff_s: float = _INITIAL_BACKOFF_S,
        max_backoff_s: float = _MAX_BACKOFF_S,
    ) -> None:
        if isinstance(min_spare_memory_mb, bool) or not isinstance(min_spare_memory_mb, int):
            raise ValueError("min_spare_memory_mb must be an integer")
        if min_spare_memory_mb < 0:
            raise ValueError("min_spare_memory_mb must be non-negative")
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
        self.min_spare_memory_mb = min_spare_memory_mb
        self.launch_min_interval_s = float(launch_min_interval_s)
        self._spare_reader = spare_reader
        self._sleep = sleep
        self._clock = clock
        self._initial_backoff_s = initial_backoff_s
        self._max_backoff_s = max_backoff_s
        self._last_launch: float | None = None

    async def wait_for_headroom(self, *, trial: str, task: str) -> None:
        backoff = self._initial_backoff_s
        attempts = 0
        while True:
            try:
                spare_mb = int(await asyncio.to_thread(self._spare_reader))
            except (OSError, TypeError, ValueError) as exc:
                raise RuntimeError(str(exc)) from exc
            now = self._clock()
            enough_memory = spare_is_sufficient(spare_mb, self.min_spare_memory_mb)
            since_last = None if self._last_launch is None else now - self._last_launch
            interval_passed = since_last is None or since_last >= self.launch_min_interval_s
            if enough_memory and interval_passed:
                if attempts:
                    logger.info("launch gate: admitting %s after %d deferral(s)", trial, attempts)
                self._last_launch = now
                return
            attempts += 1
            if attempts == 1:
                if not enough_memory:
                    logger.warning(
                        "launch gate: deferring %s (task %s): spare %d MB < limit %d MB",
                        trial,
                        task,
                        spare_mb,
                        self.min_spare_memory_mb,
                    )
                else:
                    logger.warning(
                        "launch gate: pacing %s (%.1fs since last launch)", trial, since_last
                    )
            else:
                logger.debug("launch gate: still deferring %s (attempt %d)", trial, attempts)
            await self._sleep(backoff)
            backoff = min(self._max_backoff_s, backoff * 2)


def _post(url: str, body: bytes) -> int:
    request = Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
    with urlopen(request, timeout=10) as response:
        return response.status


def sort_slowest_first(
    configs: Sequence[TrialConfig], timeout_sec: Callable[[TrialConfig], float]
) -> list[TrialConfig]:
    return sorted(configs, key=lambda config: -timeout_sec(config))


class RunPlugin(BaseJobPlugin):
    def __init__(self, proxy_url: str, limits_file: str | Path, task_order: str) -> None:
        super().__init__(proxy_url=proxy_url, limits_file=limits_file, task_order=task_order)
        if task_order not in {"as_listed", "slowest_first"}:
            raise ValueError(f"task_order must be as_listed or slowest_first; got {task_order!r}")
        proxy_config = read_proxy_config(Path(limits_file))
        limits = Limits.from_row(proxy_config.limits)
        self.proxy_url = proxy_url.rstrip("/")
        self.task_order = task_order
        self._gate = RamSpareGate(
            min_spare_memory_mb=limits.min_spare_memory_mb,
            launch_min_interval_s=limits.launch_min_interval_s,
        )

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
        if task.path is not None:
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

    async def _on_trial_start(self, event: TrialHookEvent) -> None:
        await self._gate.wait_for_headroom(trial=event.trial_name, task=event.task_name)

    async def _on_agent_start(self, event: TrialHookEvent) -> None:
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

    async def on_job_start(self, job: Job) -> None:
        if self.task_order == "slowest_first":
            job._remaining_trial_configs = sort_slowest_first(
                job._remaining_trial_configs,
                lambda config: self._timeout_sec(job, config),
            )
        job.add_hook(TrialEvent.START, self._on_trial_start)
        job.add_hook(TrialEvent.AGENT_START, self._on_agent_start)
        job.add_hook(TrialEvent.AGENT_END, self._on_agent_end)

    async def on_job_end(self, job_result: JobResult) -> None:
        del job_result
