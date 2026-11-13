from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path

from harbor_research_runner.docker_host import container_memory
from harbor_research_runner.docker_host.lifeline import start_lifeline_watch
from harbor_research_runner.harbor_adapter import job_folder
from harbor_research_runner.harbor_adapter.folders import HarborFolderError
from harbor_research_runner.records.jsonl import append_row, open_for_append
from harbor_research_runner.records.machine_check import read as read_machine_check
from harbor_research_runner.run.canary import canary_memory_mb
from harbor_research_runner.trial_validity.verdict import TrialFacts, compose_project


def _trial_memory_data(
    run_dir: Path, trial_cache: dict[str, TrialFacts] | None = None
) -> tuple[dict[str, int | None], dict[str, str]]:
    check = read_machine_check(run_dir / "machine-check.json")
    limits = {row["name"]: row.get("memory_mb") for row in check.tasks}
    trial_limits = {}
    trial_locations = {}
    trial_cache = {} if trial_cache is None else trial_cache
    for location in ("harbor-job", "canary-job"):
        location_dir = run_dir / location
        try:
            folders = [folder for folder in location_dir.iterdir() if folder.is_dir()]
        except FileNotFoundError:
            continue
        for folder in folders:
            try:
                facts = trial_cache.get(folder.name)
                if facts is None:
                    facts = job_folder.read_trial(folder)
                    trial_cache[folder.name] = facts
            except (FileNotFoundError, json.JSONDecodeError, KeyError, HarborFolderError):
                continue
            if location == "canary-job":
                limit_mb = canary_memory_mb()
            else:
                if facts.task not in limits:
                    raise ValueError(f"machine check has no memory limit for task {facts.task!r}")
                limit_mb = limits[facts.task]
            trial_limits[facts.trial] = limit_mb
            trial_locations[facts.trial] = location
    return trial_limits, trial_locations


def trial_memory_limits(run_dir: Path) -> dict[str, int | None]:
    return _trial_memory_data(run_dir)[0]


def kill_decision(container: str, used_mb: float, limit_mb: int | None, killed: set[str]) -> bool:
    if limit_mb is None or used_mb <= limit_mb or container in killed:
        return False
    killed.add(container)
    return True


def sample_once(
    run_dir: Path,
    out_fd: int,
    memory_cap: str,
    *,
    now: Callable[[], float] = time.time,
    run: Callable = container_memory._run,
    killed: set[str] | None = None,
    trial_cache: dict[str, TrialFacts] | None = None,
) -> None:
    limits, locations = _trial_memory_data(run_dir, trial_cache)
    trial_by_project = {compose_project(trial): trial for trial in limits}
    containers = container_memory.running_containers(run)
    reader = container_memory.reader_for_memory_cap(memory_cap)
    stats = (
        container_memory.docker_stats_memory(run, containers) if reader == "docker_stats" else {}
    )
    killed = set() if killed is None else killed
    for container in containers:
        project = container_memory.compose_project_label(container)
        trial = trial_by_project.get(project)
        if trial is None:
            continue
        container_id = str(container.get("ID"))
        try:
            if reader == "docker_stats":
                used_mb = stats.get(container_id)
                used_reader = reader
            else:
                used_mb, used_reader = container_memory.proc_container_memory(container_id, run=run)
        except (subprocess.CalledProcessError, ValueError, OSError):
            continue
        if used_mb is None:
            continue
        limit_mb = limits[trial]
        row = {
            "type": "sample",
            "t": now(),
            "trial": trial,
            "location": locations[trial],
            "container": container_id,
            "used_mb": used_mb,
            "limit_mb": limit_mb,
            "reader": used_reader,
        }
        should_kill = limit_mb is not None and used_mb > limit_mb and container_id not in killed
        if should_kill:
            try:
                container_memory.kill_container(container_id, run)
            except (subprocess.CalledProcessError, ValueError, OSError):
                continue
            killed.add(container_id)
        append_row(out_fd, row)
        if should_kill:
            append_row(
                out_fd,
                {**row, "type": "kill"},
            )


def run_memory_watch(
    run_dir: Path,
    memory_cap: str,
    lifeline_fd: int | None = None,
    *,
    now: Callable[[], float] = time.time,
    wait: Callable[[threading.Event, float], object] = threading.Event.wait,
) -> int:
    stopped = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stopped.set())
    start_lifeline_watch(lifeline_fd, stopped)
    out_fd = open_for_append(run_dir / "memory-watch.jsonl")
    killed: set[str] = set()
    trial_cache: dict[str, TrialFacts] = {}
    try:
        while not stopped.is_set():
            sample_once(
                run_dir,
                out_fd,
                memory_cap,
                now=now,
                killed=killed,
                trial_cache=trial_cache,
            )
            wait(stopped, 2.0)
        return 0
    finally:
        os.close(out_fd)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--memory-cap", choices=("kernel", "none"), required=True)
    parser.add_argument("--lifeline-fd", type=int)
    args = parser.parse_args(argv)
    return run_memory_watch(args.run_dir, args.memory_cap, args.lifeline_fd)


if __name__ == "__main__":
    raise SystemExit(main())
