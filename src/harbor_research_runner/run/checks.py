from __future__ import annotations

import socket
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from harbor_research_runner.docker_host.cleanup import (
    Inventory,
)
from harbor_research_runner.docker_host.host_lock import (
    HostLock,
    HostLockUnavailable,
    HostLockUnsupported,
)
from harbor_research_runner.docker_host.processes import pids_naming_run
from harbor_research_runner.docker_host.profile import profile_differences
from harbor_research_runner.harness.manifest import (
    ImageEntry,
    ManifestError,
    local_image_digest,
    verify_arm,
)
from harbor_research_runner.harness.request_path import request_path
from harbor_research_runner.job_config.job_file import Job
from harbor_research_runner.job_config.model_cards import ModelCard
from harbor_research_runner.records.limits import Limits, host_breach
from harbor_research_runner.records.machine_check import MachineCheck
from harbor_research_runner.records.resource_stats import Sample
from harbor_research_runner.run.canary import canary_memory_mb


def _bind_port(host: str, port: int) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((host, port))


@dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool
    detail: str


@dataclass(frozen=True)
class CheckDeps:
    select_adapter: Callable[[str, str], object]
    measure: Callable[[], tuple[Sample, int]]
    inventory: Callable[[], Inventory]
    live_run: Callable[[Path], str | None]
    host_lock: HostLock
    read_machine_check: Callable[[Path], MachineCheck]
    host_fingerprint: Callable[[], str]
    harbor_version: Callable[[], str]
    docker_memory_mb: Callable[[], int]
    host_profile: Callable[[], dict] = lambda: {}
    bind_port: Callable[[str, int], None] = _bind_port
    processes: Callable[[Path], list[int]] = pids_naming_run
    digest_of: Callable[[str], str | None] = local_image_digest


def _evaluate(name: str, check: Callable[[], tuple[bool, str]]) -> CheckResult:
    try:
        ok, detail = check()
    except Exception as exc:
        detail = str(exc) or exc.__class__.__name__
        return CheckResult(name, False, detail)
    return CheckResult(name, ok, detail)


def docker_clean_check(deps: CheckDeps) -> tuple[bool, str]:
    inventory = deps.inventory()
    if not inventory.daemon_ok:
        return False, inventory.daemon_detail
    residue = (*inventory.residue_containers, *inventory.leaked_networks)
    if residue:
        return False, f"Docker residue found: {', '.join(residue)}"
    return True, "Docker inventory is clean"


def single_run_check(runs_dir: Path, deps: CheckDeps) -> tuple[bool, str]:
    try:
        deps.host_lock.acquire()
    except HostLockUnsupported as exc:
        return False, f"host lock unsupported: {exc}"
    except HostLockUnavailable as exc:
        try:
            holder = deps.live_run(runs_dir)
        except Exception as lookup_exc:
            return False, f"host lock is held; holder is unknown: {lookup_exc}"
        if holder is not None and holder != "unknown":
            return False, f"host lock is held by run {holder}"
        if holder == "unknown":
            return False, f"host lock is held; holder is unknown: {exc}"
        return False, f"host lock is held by another process: {exc}"
    return True, "this process holds the host lock"


def host_resources_check(job: Job, deps: CheckDeps, multiplier: float) -> tuple[bool, str]:
    limits = Limits.from_dict(job.limit_overrides, job.repetitions, multiplier)
    sample, _ = deps.measure()
    if host_breach(sample.to_row(), limits):
        return False, "host resource limits are breached"
    return True, "host resources meet the job limits"


def proxy_port_free_check(job: Job, deps: CheckDeps) -> tuple[bool, str]:
    try:
        deps.bind_port(job.proxy_host, job.proxy_port)
    except OSError:
        return False, f"port {job.proxy_port} in use; a previous run's proxy may still be draining"
    return True, f"proxy port {job.proxy_port} is free"


def run_folder_not_live_check(run_dir: Path | None, deps: CheckDeps) -> tuple[bool, str]:
    if run_dir is None:
        return True, "new run folder is not live"
    pids = deps.processes(run_dir)
    if pids:
        return False, f"processes name run folder {run_dir}: {', '.join(map(str, pids))}"
    return True, f"no process names run folder {run_dir}"


def machine_check_launch_checks(job: Job, runs_dir: Path, deps: CheckDeps) -> list[CheckResult]:
    return [
        _evaluate("docker_clean", lambda: docker_clean_check(deps)),
        _evaluate("single_run", lambda: single_run_check(runs_dir, deps)),
        _evaluate("host_resources", lambda: host_resources_check(job, deps, 1.0)),
    ]


def run_checks(
    job: Job,
    card: ModelCard,
    manifest: dict[tuple[str, str, str], ImageEntry],
    arch: str,
    runs_dir: Path,
    environment: Mapping[str, str],
    deps: CheckDeps,
    *,
    run_dir: Path | None = None,
) -> list[CheckResult]:
    try:
        machine_check = deps.read_machine_check(job.machine_check)
        machine_check_error = ""
    except Exception as exc:
        machine_check = None
        machine_check_error = str(exc) or exc.__class__.__name__

    def model_card_check() -> tuple[bool, str]:
        if card.withdrawn:
            return False, card.reason or "model card is withdrawn"
        return True, "model card is available"

    def keys_present_check() -> tuple[bool, str]:
        missing = [name for name in card.key_env if not environment.get(name)]
        if missing:
            return False, f"unset or empty environment variable(s): {', '.join(missing)}"
        return True, "all model key variables are set"

    def arms_pinned_check() -> tuple[bool, str]:
        problems: list[str] = []
        arches = {arch}
        if machine_check is not None:
            task_arches = {task["name"]: task.get("arch", arch) for task in machine_check.tasks}
            arches.update(
                task_arches[name]
                for name in resolve_tasks(job, machine_check)
                if name in task_arches
            )
        for arm in job.arms:
            if arm.harness != "fake":
                for required_arch in sorted(arches):
                    try:
                        verify_arm(manifest, arm.harness, arm.commit, required_arch, deps.digest_of)
                    except ManifestError as exc:
                        problems.append(str(exc) or exc.__class__.__name__)
            try:
                deps.select_adapter(card.api, request_path(arm.harness))
            except ValueError as exc:
                problems.append(str(exc))
        if problems:
            return False, "; ".join(problems)
        return True, "all arm images and harness adapter pairs are valid"

    def machine_check_present_check() -> tuple[bool, str]:
        if machine_check is None:
            return False, f"machine check is missing or unparseable: {machine_check_error}"
        return True, "machine check is present and parseable"

    def machine_check_matches_check() -> tuple[bool, str]:
        if machine_check is None:
            return False, "machine check is unavailable"
        mismatches: list[str] = []
        if machine_check.task_source != _task_source(job):
            mismatches.append("task source differs")
        if machine_check.harbor_version != deps.harbor_version():
            mismatches.append("Harbor version differs")
        if machine_check.host_profile is None:
            mismatches.append(
                "the machine check predates host profile recording; rerun `hrr check-machine`"
            )
        else:
            mismatches.extend(profile_differences(machine_check.host_profile, deps.host_profile()))
        if job.concurrency > machine_check.concurrency:
            mismatches.append(
                f"job concurrency {job.concurrency} exceeds checked concurrency "
                f"{machine_check.concurrency}"
            )
        if mismatches:
            return False, "; ".join(mismatches)
        return True, "machine check matches this job and host"

    def tasks_included_check() -> tuple[bool, str]:
        if not job.tasks.include:
            return True, "empty task include selects all machine-check included tasks"
        included = set(machine_check.included if machine_check is not None else ())
        missing = [task for task in job.tasks.include if task not in included]
        if missing:
            return False, f"task(s) are not included in the machine check: {', '.join(missing)}"
        return True, "all requested tasks are included in the machine check"

    def memory_fits_check() -> tuple[bool, str]:
        limits = Limits.from_dict(job.limit_overrides, job.repetitions, 1.0)
        task_names = resolve_tasks(job, machine_check) if machine_check is not None else []
        task_memory: dict[str, int | None] = {}
        for task in machine_check.tasks if machine_check is not None else []:
            name = task.get("name")
            value = task.get("memory_mb")
            if isinstance(name, str):
                task_memory[name] = (
                    value if isinstance(value, int) and not isinstance(value, bool) else None
                )
        missing: list[str] = []
        memory_values: list[int] = []
        for name in task_names:
            value = task_memory.get(name)
            if value is None:
                missing.append(name)
            else:
                memory_values.append(value)
        memory_values.append(canary_memory_mb())
        max_task_memory = max(memory_values) if not missing else None
        concurrency = job.concurrency
        reserve = limits.memory_reserve_mb
        try:
            docker_memory = deps.docker_memory_mb()
        except Exception as exc:
            detail = (
                f"concurrency={concurrency}, max_task_memory_mb={max_task_memory}, "
                f"memory_reserve_mb={reserve}, docker_memory_mb=unavailable; "
                f"could not read Docker memory: {str(exc) or exc.__class__.__name__}"
            )
            return False, detail
        numbers = (
            f"concurrency={concurrency}, max_task_memory_mb={max_task_memory}, "
            f"memory_reserve_mb={reserve}, docker_memory_mb={docker_memory}"
        )
        if machine_check is not None and any("memory_mb" not in t for t in machine_check.tasks):
            return (
                False,
                "the machine check predates task memory recording; rerun `hrr check-machine`",
            )
        if missing:
            names = ", ".join(missing)
            return (
                False,
                f"task {names} declares no memory limit; its container would run unbounded; {numbers}",
            )
        if machine_check is None:
            return False, f"machine check is unavailable; {numbers}"
        if max_task_memory is None:
            return False, f"task memory could not be determined; {numbers}"
        required = concurrency * max_task_memory + reserve
        if required > docker_memory:
            return False, f"required_memory_mb={required} exceeds Docker memory; {numbers}"
        return True, f"required_memory_mb={required} fits Docker memory; {numbers}"

    return [
        _evaluate("model_card", model_card_check),
        _evaluate("keys_present", keys_present_check),
        _evaluate("arms_pinned", arms_pinned_check),
        _evaluate("docker_clean", lambda: docker_clean_check(deps)),
        _evaluate("proxy_port_free", lambda: proxy_port_free_check(job, deps)),
        _evaluate("run_folder_not_live", lambda: run_folder_not_live_check(run_dir, deps)),
        _evaluate("single_run", lambda: single_run_check(runs_dir, deps)),
        _evaluate(
            "host_resources",
            lambda: host_resources_check(
                job,
                deps,
                machine_check.verifier_timeout_multiplier if machine_check else 1.0,
            ),
        ),
        _evaluate("machine_check_present", machine_check_present_check),
        _evaluate("machine_check_matches", machine_check_matches_check),
        _evaluate("tasks_included", tasks_included_check),
        _evaluate("memory_fits", memory_fits_check),
    ]


def resolve_tasks(job: Job, machine_check: MachineCheck) -> list[str]:
    included = set(job.tasks.include)
    excluded = set(job.tasks.exclude)
    return [
        task
        for task in machine_check.included
        if task not in excluded and (not included or task in included)
    ]


def _task_source(job: Job) -> dict:
    tasks = job.tasks
    if tasks.kind == "dataset":
        return {"dataset": tasks.dataset}
    if tasks.kind == "path":
        return {"path": tasks.path}
    if tasks.kind == "git":
        return {"git": {"url": tasks.git_url, "commit": tasks.git_commit}}
    raise ValueError(f"unknown task source kind {tasks.kind!r}")
