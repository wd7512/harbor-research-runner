from __future__ import annotations

import importlib
import os
import pkgutil
import socket
import subprocess
import time
from pathlib import Path

import pytest

TESTS_ROOT = Path(__file__).resolve().parent


def _preimport_package(name: str) -> None:
    package = importlib.import_module(name)
    for module in pkgutil.walk_packages(package.__path__, f"{name}."):
        try:
            importlib.import_module(module.name)
        except ImportError:
            continue


_preimport_package("harbor_research_runner")


def _warm_lazy_imports() -> None:
    try:
        import httpx
    except ImportError:
        return
    httpx.Client().close()


_warm_lazy_imports()

REFERENCE_CALIBRATION_S = 0.0067
MAX_SPEED_FACTOR = 4.0


def _calibrate_cpu_speed() -> float:
    best = float("inf")
    for _ in range(5):
        start = time.process_time()
        total = 0
        for i in range(300_000):
            total += i * i
        table = {}
        for i in range(30_000):
            table[str(i)] = i
        best = min(best, time.process_time() - start)
    return min(MAX_SPEED_FACTOR, max(1.0, best / REFERENCE_CALIBRATION_S))


CPU_SPEED_FACTOR = _calibrate_cpu_speed()

CPU = "cpu"
WALL = "wall"

BUDGETS: tuple[tuple[str, float, str], ...] = (
    ("unit", 0.1, CPU),
    ("integration", 5.0, WALL),
    ("e2e", 300.0, WALL),
)

_STARTED = pytest.StashKey[tuple[float, float]]()


def _load_factor() -> float:
    try:
        return max(1.0, os.getloadavg()[0] / (os.cpu_count() or 1))
    except OSError:
        return 1.0


def tier_of(path: Path) -> str | None:
    try:
        parts = path.resolve().relative_to(TESTS_ROOT).parts
    except ValueError:
        return None
    return parts[0] if len(parts) > 1 else None


def budget_for(path: Path) -> tuple[float, str] | None:
    tier = tier_of(path)
    for name, cap, clock in BUDGETS:
        if tier == name:
            return cap, clock
    return None


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_protocol(item: pytest.Item, nextitem: pytest.Item | None):
    item.stash[_STARTED] = (time.process_time(), time.perf_counter())
    return (yield)


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[None]):
    report = yield
    budget = budget_for(Path(str(item.path)))
    if budget is None or call.when != "teardown" or report.outcome != "passed":
        return report
    cap, clock = budget
    cap *= _load_factor() if clock == WALL else CPU_SPEED_FACTOR
    cpu_start, wall_start = item.stash[_STARTED]
    spent = time.process_time() - cpu_start if clock == CPU else time.perf_counter() - wall_start
    if spent > cap:
        report.outcome = "failed"
        report.longrepr = (
            f"{item.nodeid}: setup+call+teardown used {spent:.3f}s of {clock} time, "
            f"over the {cap:.2f}s budget"
        )
    return report


def _banned(what: str):
    def refuse(*args, **kwargs):
        raise AssertionError(
            f"{what} is not allowed in a unit test: pass a fake in, or move the test to tests/integration"
        )

    return refuse


@pytest.fixture(autouse=True)
def _no_sleep_process_or_network(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch):
    if tier_of(Path(str(request.node.path))) != "unit":
        return
    monkeypatch.setattr(time, "sleep", _banned("time.sleep"))
    monkeypatch.setattr(subprocess, "Popen", _banned("subprocess.Popen"))
    monkeypatch.setattr(os, "fork", _banned("os.fork"))
    monkeypatch.setattr(socket.socket, "connect", _banned("socket connect"))
    monkeypatch.setattr(socket.socket, "bind", _banned("socket bind"))
