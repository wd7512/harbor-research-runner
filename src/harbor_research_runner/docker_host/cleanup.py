from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from harbor_research_runner.docker_host.host_lock import (
    HostLock,
    HostLockUnavailable,
    HostLockUnsupported,
)
from harbor_research_runner.docker_host.processes import pids_naming_run
from harbor_research_runner.records.jsonl import read_rows

logger = logging.getLogger(__name__)

DOCKER_TIMEOUT_S = 30
CLEAN_COMMAND = "hrr clean-docker --apply"
CONTAINER_RESIDUE_RE = re.compile(r"__env-main-\d+$")
NETWORK_RESIDUE_RE = re.compile(r"__env_default$")
COMPOSE_ILLEGAL_RE = re.compile(r"[^a-z0-9_-]")
POOL_EXHAUSTED_RE = re.compile(
    r"all predefined address pools have been fully subnetted", re.IGNORECASE
)


class DockerDown(Exception):
    pass


class RunActive(Exception):
    pass


@dataclass(frozen=True)
class Inventory:
    daemon_ok: bool
    daemon_detail: str
    residue_containers: tuple[str, ...]
    leaked_networks: tuple[str, ...]
    removable_networks: tuple[str, ...] = ()
    skipped_attached: tuple[str, ...] = ()


@dataclass(frozen=True)
class Plan:
    planned_containers: tuple[str, ...]
    planned_networks: tuple[str, ...]
    skipped_attached: tuple[str, ...]
    refusal: str | None = None


@dataclass(frozen=True)
class Report:
    dry_run: bool
    planned_containers: tuple[str, ...]
    planned_networks: tuple[str, ...]
    removed_containers: tuple[str, ...]
    removed_networks: tuple[str, ...]
    skipped_attached: tuple[str, ...]


def compose_project_name(trial: str) -> str:
    name = trial.lower()
    if not re.match(r"^[a-z0-9]", name):
        name = "0" + name
    return COMPOSE_ILLEGAL_RE.sub("-", name)


def is_residue_container(name: str) -> bool:
    return CONTAINER_RESIDUE_RE.search(name) is not None


def is_leaked_network(name: str) -> bool:
    return NETWORK_RESIDUE_RE.search(name) is not None


def is_pool_exhaustion_text(text: str) -> bool:
    return POOL_EXHAUSTED_RE.search(text) is not None


def _run_docker(argv: list[str], run: Callable = subprocess.run):
    try:
        return run(argv, capture_output=True, text=True, timeout=DOCKER_TIMEOUT_S)
    except FileNotFoundError as exc:
        raise DockerDown(f"docker not found on PATH ({exc})") from exc
    except (subprocess.SubprocessError, OSError) as exc:
        raise DockerDown(f"docker daemon unreachable: {exc}") from exc


def _error_detail(text: str) -> str:
    detail = text.strip() or "docker command failed"
    if is_pool_exhaustion_text(detail):
        detail += f"; Docker address pool exhausted — clean with `{CLEAN_COMMAND}`"
    return detail


def docker_memory_mb(run: Callable = subprocess.run) -> int:
    try:
        info = _run_docker(["docker", "info", "--format", "{{.MemTotal}}"], run)
    except DockerDown as exc:
        raise DockerDown(f"could not read Docker memory: {exc}") from exc
    if info.returncode != 0:
        detail = _error_detail(info.stderr or info.stdout or "docker info failed")
        raise DockerDown(f"could not read Docker memory: {detail}")
    value = info.stdout.strip()
    try:
        memory_bytes = int(value)
    except ValueError as exc:
        raise DockerDown(f"Docker returned an invalid MemTotal value {value!r}") from exc
    if memory_bytes <= 0:
        raise DockerDown(f"Docker returned a non-positive MemTotal value {memory_bytes}")
    return memory_bytes // (1024**2)


def inventory(run: Callable = subprocess.run) -> Inventory:
    try:
        info = _run_docker(["docker", "info", "--format", "{{.ServerVersion}}"], run)
    except DockerDown as exc:
        return Inventory(False, str(exc), (), ())
    if info.returncode != 0:
        detail = _error_detail(info.stderr or info.stdout or "docker info failed")
        return Inventory(False, f"docker daemon unreachable: {detail}", (), ())
    try:
        ps = _run_docker(["docker", "ps", "-a", "--format", "{{.Names}}"], run)
        networks = _run_docker(["docker", "network", "ls", "--format", "{{.Name}}"], run)
    except DockerDown as exc:
        return Inventory(False, str(exc), (), ())
    if ps.returncode != 0 or networks.returncode != 0:
        detail = _error_detail(
            ps.stderr or networks.stderr or ps.stdout or networks.stdout or "listing failed"
        )
        return Inventory(False, f"docker daemon unreachable: {detail}", (), ())
    containers = tuple(
        name
        for line in ps.stdout.splitlines()
        if (name := line.strip()) and is_residue_container(name)
    )
    leaked = tuple(
        name
        for line in networks.stdout.splitlines()
        if (name := line.strip()) and is_leaked_network(name)
    )
    residue = set(containers)
    removable: list[str] = []
    skipped: list[str] = []
    for name in leaked:
        endpoints = _network_containers(name, run)
        if endpoints is None or any(endpoint not in residue for endpoint in endpoints):
            skipped.append(name)
        else:
            removable.append(name)
    return Inventory(
        True, info.stdout.strip() or "ok", containers, leaked, tuple(removable), tuple(skipped)
    )


def _network_containers(name: str, run: Callable) -> tuple[str, ...] | None:
    try:
        result = _run_docker(
            ["docker", "network", "inspect", name, "--format", "{{json .Containers}}"], run
        )
    except DockerDown:
        return None
    if result.returncode != 0:
        logger.warning("could not inspect network %s; leaving it in place", name)
        return None
    try:
        containers = json.loads(result.stdout.strip() or "{}")
    except json.JSONDecodeError:
        logger.warning("could not parse inspect output for network %s; leaving it in place", name)
        return None
    if not isinstance(containers, dict):
        return None
    return tuple(
        sorted(
            entry["Name"]
            for entry in containers.values()
            if isinstance(entry, dict) and isinstance(entry.get("Name"), str)
        )
    )


def plan(inventory: Inventory, live_run: str | None) -> Plan:
    if live_run is not None:
        return Plan((), (), (), f"run {live_run} is active")
    if not inventory.daemon_ok:
        raise DockerDown(inventory.daemon_detail)
    return Plan(
        inventory.residue_containers, inventory.removable_networks, inventory.skipped_attached
    )


def _planned(inventory_result: Inventory, live_run: str | None, run: Callable) -> Plan:
    return plan(inventory_result, live_run)


def _remove(plan_result: Plan, run: Callable) -> Report:
    removed_containers: list[str] = []
    for name in plan_result.planned_containers:
        stopped = _run_docker(["docker", "stop", name], run)
        if stopped.returncode != 0:
            logger.warning("docker stop %s exited %d", name, stopped.returncode)
        removed = _run_docker(["docker", "rm", name], run)
        if removed.returncode != 0:
            logger.warning("docker rm %s exited %d", name, removed.returncode)
        else:
            removed_containers.append(name)
    removed_networks: list[str] = []
    for name in plan_result.planned_networks:
        removed = _run_docker(["docker", "network", "rm", name], run)
        if removed.returncode != 0:
            logger.warning("docker network rm %s exited %d", name, removed.returncode)
        else:
            removed_networks.append(name)
    return Report(
        False,
        plan_result.planned_containers,
        plan_result.planned_networks,
        tuple(removed_containers),
        tuple(removed_networks),
        plan_result.skipped_attached,
    )


def _report(plan_result: Plan) -> Report:
    if plan_result.refusal:
        raise RunActive(f"refusing Docker cleanup: {plan_result.refusal}")
    return Report(
        True,
        plan_result.planned_containers,
        plan_result.planned_networks,
        (),
        (),
        plan_result.skipped_attached,
    )


def clean(
    apply: bool,
    lock: HostLock,
    run: Callable = subprocess.run,
    runs_dir: Path | None = None,
    processes: Callable[[Path], list[int]] = pids_naming_run,
) -> Report:
    if not apply:
        return _report(_planned(inventory(run), None, run))
    acquired = not lock.held
    try:
        if acquired:
            try:
                lock.acquire()
            except HostLockUnavailable as exc:
                holder = live_run(runs_dir, processes=processes) if runs_dir is not None else None
                identity = holder if holder is not None else str(lock.path)
                raise RunActive(
                    f"refusing Docker cleanup: host lock is held by {identity}"
                ) from exc
            except HostLockUnsupported:
                raise
        holder = live_run(runs_dir, processes=processes) if runs_dir is not None else None
        if holder is not None:
            raise RunActive(f"refusing Docker cleanup: run {holder} is active")
        return _remove(_planned(inventory(run), None, run), run)
    finally:
        if acquired and lock.held:
            lock.release()


def clean_trials(trial_names: frozenset[str], run: Callable = subprocess.run) -> Report:
    if not trial_names:
        return Report(False, (), (), (), (), ())
    inv = inventory(run)
    if not inv.daemon_ok:
        logger.warning(
            "trial Docker cleanup failed; residue counts left are unknown: %s", inv.daemon_detail
        )
        return Report(False, (), (), (), (), ())
    projects = {compose_project_name(trial) for trial in trial_names}
    containers = tuple(
        name
        for name in inv.residue_containers
        if any(name.startswith(f"{project}__env-main-") for project in projects)
    )
    networks = tuple(
        name
        for name in inv.leaked_networks
        if any(name == f"{project}__env_default" for project in projects)
    )
    narrowed = Inventory(
        True,
        inv.daemon_detail,
        containers,
        tuple(name for name in inv.leaked_networks if name in networks),
        tuple(name for name in inv.removable_networks if name in networks),
        tuple(name for name in inv.skipped_attached if name in networks),
    )
    planned = plan(narrowed, None)
    try:
        report = _remove(planned, run)
    except DockerDown as exc:
        logger.warning("trial Docker cleanup failed; residue counts left are unknown: %s", exc)
        return Report(False, (), (), (), (), ())
    left_containers = len(report.planned_containers) - len(report.removed_containers)
    left_networks = (
        len(report.planned_networks) - len(report.removed_networks) + len(report.skipped_attached)
    )
    if left_containers or left_networks:
        logger.warning(
            "trial Docker cleanup incomplete; %d residue container(s) and %d network(s) remain",
            left_containers,
            left_networks,
        )
    return report


def _default_pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def live_run(
    runs_dir: Path,
    pid_alive: Callable[[int], bool] = _default_pid_alive,
    processes: Callable[[Path], list[int]] = pids_naming_run,
) -> str | None:
    try:
        run_dirs = sorted(path for path in runs_dir.iterdir() if path.is_dir())
    except FileNotFoundError:
        return None
    except OSError:
        return "unknown"
    for run_dir in run_dirs:
        try:
            if processes(run_dir):
                return run_dir.name
        except Exception:
            return "unknown"
        path = run_dir / "run-events.jsonl"
        try:
            events = read_rows(path)
        except OSError:
            return "unknown"
        if events.torn_last_line or events.bad_lines:
            return "unknown"
        active: list[int] = []
        for row in events.rows:
            event_type = row.get("type")
            if not isinstance(event_type, str):
                return "unknown"
            if event_type in {"halt", "finish"}:
                active.clear()
            elif event_type in {"launch", "resume"}:
                pid = row.get("pid")
                if not isinstance(pid, int) or isinstance(pid, bool):
                    return "unknown"
                active.append(pid)
        for pid in active:
            try:
                if pid_alive(pid):
                    return run_dir.name
            except PermissionError:
                return "unknown"
            except ProcessLookupError:
                continue
            except OSError:
                return "unknown"
    return None
