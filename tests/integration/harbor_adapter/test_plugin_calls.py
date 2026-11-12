from __future__ import annotations

import asyncio
import json
import threading
import urllib.error
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import cast

import pytest
from harbor.models.trial.config import AgentConfig, TaskConfig, TrialConfig
from harbor.trial.hooks import TrialEvent, TrialHookEvent

from harbor_research_runner.harbor_adapter.plugin import RamSpareGate, RunPlugin
from harbor_research_runner.records.limits import Limits
from harbor_research_runner.records.proxy_control import ProxyConfig

TASK = "terminal-bench/bn-fit-modify"
TRIAL = "bn-fit-modify__xzmPm77"


class _ProxyServer(ThreadingHTTPServer):
    def __init__(self, handler):
        super().__init__(("127.0.0.1", 0), handler)
        self.requests: list[tuple[str, dict]] = []
        self.statuses: list[int] = []
        self.hold: threading.Event | None = None
        self.released_while_held: bool | None = None


class _ProxyHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        server = cast(_ProxyServer, self.server)
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length))
        server.requests.append((self.path, body))
        if server.hold is not None:
            server.released_while_held = server.hold.wait(timeout=2.0)
        status = server.statuses.pop(0) if server.statuses else 200
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, format: str, *args) -> None:
        return


@contextmanager
def _proxy_server() -> Iterator[tuple[str, _ProxyServer]]:
    server = _ProxyServer(_ProxyHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", server
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def _plugin(tmp_path: Path, proxy_url: str, *, interval: float = 0.0) -> RunPlugin:
    limits = Limits.from_dict({"launch_min_interval_s": interval}, 1, 1.0).to_row()
    config = ProxyConfig(
        host="127.0.0.1",
        port=8766,
        ledger_path=str(tmp_path / "proxy-ledger.jsonl"),
        bodies_dir=None,
        model={"id": "fixture/model"},
        limits=limits,
        restart_cooldown_s=0.0,
    )
    limits_file = tmp_path / "proxy-config.json"
    limits_file.write_text(config.to_json(), encoding="utf-8")
    return RunPlugin(proxy_url, str(limits_file), "as_listed")


def _event(event: TrialEvent, kwargs: dict | None = None) -> TrialHookEvent:
    config = TrialConfig(
        task=TaskConfig(name=TASK),
        trial_name=TRIAL,
        agent=AgentConfig(kwargs={} if kwargs is None else kwargs),
    )
    return TrialHookEvent.model_construct(
        event=event,
        task_name=TASK,
        config=config,
        result=None,
        lock=None,
    )


class _Job:
    def __init__(self):
        self._remaining_trial_configs = []
        self._task_download_results = {}
        self.hooks = {}

    def add_hook(self, event, callback):
        self.hooks[event] = callback


async def _hooks(plugin: RunPlugin) -> dict:
    job = _Job()
    await plugin.on_job_start(job)
    return job.hooks


def test_agent_start_posts_trial_task_and_arm(tmp_path: Path) -> None:
    async def run() -> None:
        with _proxy_server() as (url, server):
            hooks = await _hooks(_plugin(tmp_path, url))
            await hooks[TrialEvent.AGENT_START](
                _event(TrialEvent.AGENT_START, {"arm_label": "major"})
            )

            assert server.requests == [
                (
                    "/control/open",
                    {"trial": TRIAL, "task": TASK, "arm": "major"},
                )
            ]

    asyncio.run(run())


def test_agent_end_posts_trial_close(tmp_path: Path) -> None:
    async def run() -> None:
        with _proxy_server() as (url, server):
            hooks = await _hooks(_plugin(tmp_path, url))
            await hooks[TrialEvent.AGENT_END](_event(TrialEvent.AGENT_END, {"arm_label": "major"}))

            assert server.requests == [("/control/close", {"trial": TRIAL})]

    asyncio.run(run())


def test_agent_start_leaves_the_event_loop_free_while_the_proxy_answers(tmp_path: Path) -> None:
    async def run() -> None:
        with _proxy_server() as (url, server):
            server.hold = threading.Event()
            hooks = await _hooks(_plugin(tmp_path, url))
            opening = asyncio.create_task(
                hooks[TrialEvent.AGENT_START](
                    _event(TrialEvent.AGENT_START, {"arm_label": "major"})
                )
            )
            while not server.requests:
                await asyncio.sleep(0.01)
            server.hold.set()
            await opening

            assert server.released_while_held is True

    asyncio.run(run())


def test_open_failure_raises_for_non_success_response(tmp_path: Path) -> None:
    async def run() -> None:
        with _proxy_server() as (url, server):
            server.statuses.append(500)
            hooks = await _hooks(_plugin(tmp_path, url))

            with pytest.raises(urllib.error.HTTPError):
                await hooks[TrialEvent.AGENT_START](
                    _event(TrialEvent.AGENT_START, {"arm_label": "major"})
                )

    asyncio.run(run())


def test_close_retries_three_times_then_logs_and_continues(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    async def run() -> None:
        with _proxy_server() as (url, server):
            server.statuses.extend([500, 500, 500])
            hooks = await _hooks(_plugin(tmp_path, url))
            await hooks[TrialEvent.AGENT_END](_event(TrialEvent.AGENT_END, {"arm_label": "major"}))

            assert server.requests == [("/control/close", {"trial": TRIAL})] * 3

    caplog.set_level("ERROR", logger="harbor_research_runner.harbor_adapter.plugin")
    asyncio.run(run())
    assert "could not close proxy trial" in caplog.text


def test_agent_without_arm_label_fails_at_agent_start_without_calling_proxy(
    tmp_path: Path,
) -> None:
    async def run() -> None:
        with _proxy_server() as (url, server):
            hooks = await _hooks(_plugin(tmp_path, url))
            with pytest.raises(ValueError, match="arm_label"):
                await hooks[TrialEvent.AGENT_START](_event(TrialEvent.AGENT_START))
            await hooks[TrialEvent.AGENT_END](_event(TrialEvent.AGENT_END))

            assert server.requests == []

    asyncio.run(run())


class _Clock:
    def __init__(self, now: float = 0.0):
        self.now = now

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds


def test_pacing_defers_then_admits_after_interval(caplog: pytest.LogCaptureFixture) -> None:
    async def run() -> None:
        clock = _Clock()
        gate = RamSpareGate(
            min_spare_memory_mb=0,
            launch_min_interval_s=20.0,
            clock=clock,
            sleep=clock.sleep,
        )
        await gate.wait_for_headroom(trial="first", task="task")
        assert clock.now == 0.0
        clock.now = 5.0

        await gate.wait_for_headroom(trial="second", task="task")

        assert clock.now >= 20.0

    caplog.set_level("WARNING", logger="harbor_research_runner.harbor_adapter.plugin")
    asyncio.run(run())
    assert "pacing second" in caplog.text


@pytest.mark.parametrize("elapsed", [20.0, 21.0])
def test_pacing_admits_at_and_after_exact_boundary_with_no_deferral(elapsed: float) -> None:
    async def run() -> None:
        clock = _Clock()

        async def must_not_sleep(_seconds: float) -> None:
            raise AssertionError("gate paced at or after the interval")

        gate = RamSpareGate(
            min_spare_memory_mb=0,
            launch_min_interval_s=20.0,
            clock=clock,
            sleep=must_not_sleep,
        )
        await gate.wait_for_headroom(trial="first", task="task")
        clock.now = elapsed

        await gate.wait_for_headroom(trial="second", task="task")

    asyncio.run(run())


def test_launch_waits_for_spare_memory() -> None:
    async def run() -> list[str]:
        launched: list[str] = []
        gate = RamSpareGate(min_spare_memory_mb=10**12)

        async def worker() -> None:
            await gate.wait_for_headroom(trial="trial-1", task=TASK)
            launched.append("trial-1")

        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(worker(), timeout=0.05)
        return launched

    assert asyncio.run(run()) == []


def test_permanently_closed_gate_queues_without_launching() -> None:
    async def run() -> list[str]:
        launched: list[str] = []
        gate = RamSpareGate(min_spare_memory_mb=10**12)

        async def worker() -> None:
            await gate.wait_for_headroom(trial="trial-1", task="slow-task")
            launched.append("trial-1")

        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(worker(), timeout=0.05)
        return launched

    assert asyncio.run(run()) == []


def test_invalid_launch_min_interval_fails_closed() -> None:
    with pytest.raises(ValueError, match="launch_min_interval_s"):
        RamSpareGate(min_spare_memory_mb=0, launch_min_interval_s=-1)


def test_non_finite_launch_min_interval_fails_closed() -> None:
    with pytest.raises(ValueError, match="launch_min_interval_s"):
        RamSpareGate(min_spare_memory_mb=0, launch_min_interval_s=float("nan"))
