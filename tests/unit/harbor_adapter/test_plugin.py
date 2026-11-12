from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from harbor.trial.hooks import TrialEvent

from harbor_research_runner.harbor_adapter.plugin import (
    MemoryProbeError,
    RunPlugin,
    parse_linux_meminfo,
    parse_macos_vm_stat,
    spare_is_sufficient,
)
from harbor_research_runner.records.limits import Limits
from harbor_research_runner.records.proxy_control import ProxyConfig


def _limits_file(tmp_path: Path) -> Path:
    limits = Limits.from_dict({}, 1, 1.0).to_row()
    config = ProxyConfig(
        host="127.0.0.1",
        port=8766,
        ledger_path=str(tmp_path / "proxy-ledger.jsonl"),
        bodies_dir=None,
        model={"id": "fixture/model"},
        limits=limits,
        restart_cooldown_s=0.0,
    )
    path = tmp_path / "proxy-config.json"
    path.write_text(config.to_json(), encoding="utf-8")
    return path


def _task(root: Path, name: str, timeout: int | float | None) -> Path:
    task_dir = root / name
    task_dir.mkdir()
    agent = "" if timeout is None else f"timeout_sec = {timeout}\n"
    (task_dir / "task.toml").write_text(
        f"[agent]\n{agent}\n[verifier]\ntimeout_sec = 10\n", encoding="utf-8"
    )
    return task_dir


def _trial_config(name: str, path: Path | None, *, task_name: str | None = None, task_id=None):
    task = SimpleNamespace(
        path=path,
        name=task_name,
        get_task_id=(lambda: task_id),
    )
    return SimpleNamespace(task=task, trial_name=name)


class _Job:
    def __init__(self, configs, downloaded=None):
        self._remaining_trial_configs = configs
        self._task_download_results = downloaded or {}
        self.hooks = []

    def add_hook(self, event, callback):
        self.hooks.append((event, callback))


def _plugin(tmp_path: Path, task_order: str = "as_listed") -> RunPlugin:
    return RunPlugin(
        proxy_url="http://127.0.0.1:8766",
        limits_file=str(_limits_file(tmp_path)),
        task_order=task_order,
    )


def test_linux_memavailable_is_spare() -> None:
    meminfo = """MemTotal:       16316524 kB
MemFree:          123456 kB
MemAvailable:    8388608 kB
Buffers:           12345 kB
"""

    assert parse_linux_meminfo(meminfo) == 8192


def test_linux_meminfo_without_memavailable_fails_closed() -> None:
    with pytest.raises(MemoryProbeError, match="MemAvailable"):
        parse_linux_meminfo("MemTotal: 1024 kB\nMemFree: 512 kB\n")


def test_macos_vm_stat_counts_free_reclaimable_pages() -> None:
    vm_stat = """Mach Virtual Memory Statistics: (page size of 4096 bytes)
Pages free:                                  1000.
Pages active:                                5000.
Pages inactive:                              2000.
Pages speculative:                            500.
Pages wired down:                             100.
Pages purgeable:                              250.
Pages occupied by compressor:                 400.
"""

    assert parse_macos_vm_stat(vm_stat) == ((1000 + 2000 + 500 + 250) * 4096) // (1024 * 1024)


def test_macos_vm_stat_explicit_page_size_wins() -> None:
    vm_stat = """Mach Virtual Memory Statistics: (page size of 4096 bytes)
Pages free:                                  1000.
Pages inactive:                              2000.
Pages speculative:                            500.
Pages purgeable:                              250.
"""

    assert parse_macos_vm_stat(vm_stat, page_size=16384) == ((1000 + 2000 + 500 + 250) * 16384) // (
        1024 * 1024
    )


def test_macos_vm_stat_without_free_pages_fails_closed() -> None:
    with pytest.raises(MemoryProbeError, match="Pages free"):
        parse_macos_vm_stat("Mach Virtual Memory Statistics: (page size of 4096 bytes)\n")


@pytest.mark.parametrize(
    ("spare_mb", "limit_mb", "expected"),
    [(2048, 2048, True), (2047, 2048, False), (2049, 2048, True)],
)
def test_spare_memory_limit_includes_the_boundary(
    spare_mb: int, limit_mb: int, expected: bool
) -> None:
    assert spare_is_sufficient(spare_mb, limit_mb) is expected


def test_run_plugin_registers_start_and_agent_hooks(tmp_path: Path) -> None:
    job = _Job([])

    asyncio.run(_plugin(tmp_path).on_job_start(job))

    assert [event for event, _ in job.hooks] == [
        TrialEvent.START,
        TrialEvent.AGENT_START,
        TrialEvent.AGENT_END,
    ]


def test_run_plugin_slowest_first_sorts_by_agent_timeout_stably(tmp_path: Path) -> None:
    fast = _task(tmp_path, "fast", 600)
    mid_a = _task(tmp_path, "mid-a", 900)
    slow = _task(tmp_path, "slow", 3600)
    mid_b = _task(tmp_path, "mid-b", 900)
    configs = [
        _trial_config("fast", fast),
        _trial_config("mid-a", mid_a),
        _trial_config("slow", slow),
        _trial_config("mid-b", mid_b),
    ]
    job = _Job(configs)

    asyncio.run(_plugin(tmp_path, "slowest_first").on_job_start(job))

    assert [config.trial_name for config in job._remaining_trial_configs] == [
        "slow",
        "mid-a",
        "mid-b",
        "fast",
    ]


def test_run_plugin_as_listed_preserves_trial_config_list_identity(tmp_path: Path) -> None:
    configs = [_trial_config("fast", None), _trial_config("slow", None)]
    job = _Job(configs)
    original = job._remaining_trial_configs

    asyncio.run(_plugin(tmp_path, "as_listed").on_job_start(job))

    assert job._remaining_trial_configs is original


def test_run_plugin_slowest_first_uses_downloaded_task_folder(tmp_path: Path) -> None:
    downloaded_task = _task(tmp_path, "downloaded", 3600)
    task_id = object()
    downloaded = SimpleNamespace(path=downloaded_task)
    config = _trial_config("downloaded", None, task_name="org/downloaded", task_id=task_id)
    job = _Job([config], {task_id: downloaded})

    asyncio.run(_plugin(tmp_path, "slowest_first").on_job_start(job))

    assert job._remaining_trial_configs == [config]


def test_run_plugin_slowest_first_refuses_unresolvable_task(tmp_path: Path) -> None:
    config = _trial_config("unresolved", None, task_name="org/unresolved", task_id="missing")
    job = _Job([config])

    with pytest.raises(ValueError, match="org/unresolved"):
        asyncio.run(_plugin(tmp_path, "slowest_first").on_job_start(job))


def test_run_plugin_slowest_first_refuses_task_without_agent_timeout(tmp_path: Path) -> None:
    task_dir = _task(tmp_path, "no-timeout", None)
    config = _trial_config("no-timeout", task_dir)
    job = _Job([config])

    with pytest.raises(ValueError, match="no-timeout"):
        asyncio.run(_plugin(tmp_path, "slowest_first").on_job_start(job))


@pytest.mark.parametrize("value", [-1, -0.5, float("nan"), float("inf"), True])
def test_invalid_launch_min_interval_fails_closed(tmp_path: Path, value: float) -> None:
    limits = Limits.from_dict({}, 1, 1.0).to_row()
    limits["launch_min_interval_s"] = value
    config = ProxyConfig(
        host="127.0.0.1",
        port=8766,
        ledger_path=str(tmp_path / "proxy-ledger.jsonl"),
        bodies_dir=None,
        model={"id": "fixture/model"},
        limits=limits,
        restart_cooldown_s=0.0,
    )
    path = tmp_path / "proxy-config.json"
    path.write_text(config.to_json(), encoding="utf-8")

    with pytest.raises(ValueError, match="launch_min_interval_s"):
        RunPlugin("http://127.0.0.1:8766", str(path), "as_listed")


def test_run_plugin_rejects_unknown_task_order(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="task_order"):
        _plugin(tmp_path, "fastest_first")
