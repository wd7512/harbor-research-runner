from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from harbor_research_runner.docker_host import container_memory
from harbor_research_runner.docker_host.lifeline import start_lifeline_watch
from harbor_research_runner.harbor_adapter import job_folder
from harbor_research_runner.harbor_adapter.folders import HarborFolderError
from harbor_research_runner.records.jsonl import append_row, open_for_append
from harbor_research_runner.records.limits import Limits, effective_memory_mb
from harbor_research_runner.records.machine_check import read as read_machine_check
from harbor_research_runner.run.canary import canary_memory_mb
from harbor_research_runner.trial_validity.verdict import TrialFacts, compose_project

logger = logging.getLogger(__name__)
MAX_CONSECUTIVE_SAMPLE_FAILURES = 30


@dataclass(frozen=True)
class TrialMemory:
    location: str
    declared_mb: int | None
    warning_mb: int | None
    kill_mb: int | None


def _read_limits(run_dir: Path) -> Limits:
    try:
        recorded = json.loads((run_dir / "run-metadata.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        recorded = {}
    if "limits" in recorded:
        return Limits.from_row(recorded["limits"])
    return Limits.from_dict({}, repetitions=1, verifier_timeout_multiplier=1.0)


def _trial_memory_data(
    run_dir: Path, policy: Limits, trial_cache: dict[str, TrialFacts] | None = None
) -> dict[str, TrialMemory]:
    check = read_machine_check(run_dir / "machine-check.json")
    limits = {row["name"]: row.get("memory_mb") for row in check.tasks}
    trials = {}
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
            warning_mb = effective_memory_mb(limit_mb, policy.memory_warn_multiplier)
            kill_mb = effective_memory_mb(limit_mb, policy.memory_kill_multiplier)
            trials[facts.trial] = TrialMemory(
                location, limit_mb, warning_mb if warning_mb != kill_mb else None, kill_mb
            )
    return trials


def trial_memory_limits(run_dir: Path) -> dict[str, int | None]:
    return {
        trial: memory.kill_mb
        for trial, memory in _trial_memory_data(run_dir, _read_limits(run_dir)).items()
    }


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
    warned: set[str] | None = None,
    trial_cache: dict[str, TrialFacts] | None = None,
    policy: Limits | None = None,
    read_text=Path.read_text,
    list_dir: Callable[[Path], list[Path]] = lambda path: list(path.iterdir()),
    readlink: Callable[[Path], str] = os.readlink,
) -> None:
    trials = _trial_memory_data(run_dir, policy or _read_limits(run_dir), trial_cache)
    trial_by_project = {compose_project(trial): trial for trial in trials}
    containers = container_memory.running_containers(run)
    reader = container_memory.reader_for_memory_cap(memory_cap)
    stats = (
        container_memory.docker_stats_memory(run, containers) if reader == "docker_stats" else {}
    )
    killed = set() if killed is None else killed
    warned = set() if warned is None else warned
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
                processes = None
            else:
                used_mb, used_reader, processes = container_memory.proc_container_memory(
                    container_id,
                    run=run,
                    read_text=read_text,
                    list_dir=list_dir,
                    readlink=readlink,
                )
        except (subprocess.CalledProcessError, ValueError, OSError):
            continue
        if used_mb is None:
            continue
        memory = trials[trial]
        row = {
            "type": "sample",
            "t": now(),
            "trial": trial,
            "location": memory.location,
            "container": container_id,
            "used_mb": used_mb,
            "limit_mb": memory.kill_mb,
            "declared_limit_mb": memory.declared_mb,
            "reader": used_reader,
        }
        append_row(out_fd, row)
        if container_id in killed:
            continue
        should_kill = kill_decision(container_id, used_mb, memory.kill_mb, killed)
        warning_mb = memory.warning_mb
        if (
            warning_mb is not None
            and used_mb > warning_mb
            and not should_kill
            and container_id not in warned
        ):
            warned.add(container_id)
            append_row(out_fd, {**row, "type": "warning", "warning_limit_mb": warning_mb})
        if should_kill:
            over_limit = {**row, "type": "over_limit"}
            try:
                if reader == "docker_stats":
                    processes = container_memory.docker_top_processes(container_id, run)
                over_limit["processes"] = processes[:20] if processes is not None else None
            except (subprocess.CalledProcessError, ValueError, OSError) as exc:
                over_limit["processes"] = None
                over_limit["processes_error"] = str(exc)
            append_row(out_fd, over_limit)
            try:
                container_memory.kill_container(container_id, run)
            except (subprocess.CalledProcessError, ValueError, OSError) as exc:
                append_row(out_fd, {**row, "type": "kill_failed", "error": str(exc)[:200]})
            else:
                append_row(out_fd, {**row, "type": "kill"})


def run_memory_watch(
    run_dir: Path,
    memory_cap: str,
    lifeline_fd: int | None = None,
    *,
    now: Callable[[], float] = time.time,
    wait: Callable[[threading.Event, float], object] = threading.Event.wait,
    run: Callable = container_memory._run,
) -> int:
    stopped = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stopped.set())
    start_lifeline_watch(lifeline_fd, stopped)
    policy = _read_limits(run_dir)
    out_fd = open_for_append(run_dir / "memory-watch.jsonl")
    killed: set[str] = set()
    warned: set[str] = set()
    trial_cache: dict[str, TrialFacts] = {}
    consecutive_failures = 0
    try:
        while not stopped.is_set():
            try:
                sample_once(
                    run_dir,
                    out_fd,
                    memory_cap,
                    now=now,
                    run=run,
                    killed=killed,
                    warned=warned,
                    trial_cache=trial_cache,
                    policy=policy,
                )
            except (subprocess.CalledProcessError, OSError) as exc:
                consecutive_failures += 1
                logger.warning("memory watcher sample failed: %s", exc)
                if consecutive_failures >= MAX_CONSECUTIVE_SAMPLE_FAILURES:
                    return 1
            else:
                consecutive_failures = 0
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
