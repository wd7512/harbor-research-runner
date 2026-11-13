from __future__ import annotations

import logging
import math
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path

from harbor_research_runner.harbor_adapter import job_folder
from harbor_research_runner.harbor_adapter.commands import (
    apply_host_policy,
    machine_check_command,
    oracle_job_config,
    write_job_config,
)
from harbor_research_runner.harbor_adapter.task_files import task_tomls
from harbor_research_runner.harness.manifest import host_arch, local_image_arch
from harbor_research_runner.job_config.job_file import Job
from harbor_research_runner.records.machine_check import MachineCheck, write
from harbor_research_runner.run.checks import (
    CheckDeps,
    machine_check_launch_checks,
    task_source_record,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MachineCheckDeps:
    checks: CheckDeps
    run_harbor: Callable[[list[str], Path], int]
    now: Callable[[], float]
    host_arch: Callable[[], str] = host_arch
    image_arch: Callable[[str], str] = local_image_arch


def verifier_multiplier(tasks: list[dict]) -> float:
    included = set(included_tasks(tasks))
    ratios = [
        task["verifier_s"] / task["verifier_budget_s"] for task in tasks if task["name"] in included
    ]
    largest = max(ratios, default=0.0)
    return max(1.0, math.ceil(largest * 4) / 2)


def included_tasks(tasks: list[dict]) -> list[str]:
    return [
        task["name"]
        for task in tasks
        if task["oracle_reward"] == 1.0
        and (task["nothing_reward"] is None or task["nothing_reward"] == 0.0)
    ]


def task_arches(
    task_toml_texts: Mapping[str, str],
    digests: Mapping[str, str],
    docker_host_arch: str,
    image_arch: Callable[[str], str] = local_image_arch,
    images: str = "prebuilt",
) -> dict[str, str]:
    arches: dict[str, str] = {}
    for name in digests:
        try:
            task_toml = task_toml_texts[name]
        except KeyError as exc:
            raise ValueError(f"task {name!r} has no task.toml") from exc
        try:
            task_data = tomllib.loads(task_toml)
        except tomllib.TOMLDecodeError as exc:
            raise ValueError(f"task {name!r} has invalid task.toml: {exc}") from exc
        environment = task_data.get("environment", {})
        if not isinstance(environment, dict):
            raise ValueError(f"task {name!r} has invalid [environment] in task.toml")
        image = environment.get("docker_image")
        if image is None or images == "build":
            arches[name] = docker_host_arch
            continue
        if not isinstance(image, str) or not image:
            raise ValueError(f"task {name!r} has invalid environment.docker_image {image!r}")
        try:
            arches[name] = image_arch(image)
        except Exception as exc:
            raise ValueError(
                f"could not determine architecture for task {name!r} image {image!r}: {exc}"
            ) from exc
    return arches


def task_memories(
    task_toml_texts: Mapping[str, str], digests: Mapping[str, str]
) -> dict[str, int | None]:
    memories: dict[str, int | None] = {}
    for name in digests:
        try:
            task_toml = task_toml_texts[name]
        except KeyError as exc:
            raise ValueError(f"task {name!r} has no task.toml") from exc
        try:
            task_data = tomllib.loads(task_toml)
        except tomllib.TOMLDecodeError as exc:
            raise ValueError(f"task {name!r} has invalid task.toml: {exc}") from exc
        environment = task_data.get("environment", {})
        if not isinstance(environment, dict):
            raise ValueError(f"task {name!r} has invalid [environment] in task.toml")
        memory = environment.get("memory_mb")
        if memory is not None and (isinstance(memory, bool) or not isinstance(memory, int)):
            raise ValueError(f"task {name!r} has invalid environment.memory_mb {memory!r}")
        memories[name] = memory
    return memories


def run_machine_check(
    job: Job, out: Path, with_do_nothing: bool, runs_dir: Path, deps: MachineCheckDeps
) -> MachineCheck:
    checks = machine_check_launch_checks(job, runs_dir, deps.checks)
    failed = [f"{check.name}: {check.detail}" for check in checks if not check.ok]
    if failed:
        raise RuntimeError("machine check refused: " + "; ".join(failed))

    created_t = deps.now()
    created_at = datetime.fromtimestamp(created_t, UTC)
    run_id = f"{job.name}-{created_at.strftime('%Y%m%dT%H%M%S%fZ')}"
    check_dir = runs_dir / run_id
    check_dir.mkdir(parents=True, exist_ok=False)
    task_names = list(job.tasks.include)
    oracle_name = "harbor-job"
    oracle_config_name = "oracle-job-config.yaml"
    profile = deps.checks.host_profile()
    oracle_config = oracle_job_config(job, task_names, "oracle", oracle_name)
    apply_host_policy(
        oracle_config,
        profile,
        job.tasks.images,
        check_dir / "compose-host-network.yaml",
    )
    write_job_config(check_dir / oracle_config_name, oracle_config)
    command = machine_check_command(check_dir, oracle_name, oracle_config_name)
    if deps.run_harbor(command, check_dir) != 0:
        raise RuntimeError("Harbor oracle machine-check job failed")

    oracle_dir = check_dir / oracle_name
    oracle_trials = {
        facts.task: facts
        for facts in (
            job_folder.read_trial(path, arm="oracle")
            for path in job_folder.trial_folders(oracle_dir)
        )
    }
    task_toml_texts = task_tomls(oracle_dir)
    task_budgets = job_folder.task_budgets(oracle_dir, task_toml_texts)
    agent_budgets = {name: agent_budget for name, (agent_budget, _) in task_budgets.items()}
    verifier_budgets = {
        name: verifier_budget for name, (_, verifier_budget) in task_budgets.items()
    }
    digests = job_folder.task_digests(oracle_dir)
    docker_host_arch = deps.host_arch()
    arches = task_arches(
        task_toml_texts, digests, docker_host_arch, deps.image_arch, job.tasks.images
    )
    memories = task_memories(task_toml_texts, digests)

    nothing_rewards: dict[str, float | None] = {name: None for name in oracle_trials}
    if with_do_nothing:
        nop_name = "nop-job"
        nop_config_name = "nop-job-config.yaml"
        config = oracle_job_config(job, task_names, "nop", nop_name)
        apply_host_policy(
            config,
            profile,
            job.tasks.images,
            check_dir / "compose-host-network.yaml",
        )
        write_job_config(check_dir / nop_config_name, config)
        command = machine_check_command(check_dir, nop_name, nop_config_name)
        if deps.run_harbor(command, check_dir) != 0:
            raise RuntimeError("Harbor do-nothing machine-check job failed")
        nop_trials = {
            facts.task: facts
            for facts in (
                job_folder.read_trial(path, arm="nop")
                for path in job_folder.trial_folders(check_dir / nop_name)
            )
        }
        nothing_rewards = {
            name: nop_trials[name].reward if name in nop_trials else None
            for name in set(nop_trials) | set(oracle_trials)
        }

    tasks = []
    for name, facts in sorted(oracle_trials.items()):
        agent_start = facts.phase_started.get("agent")
        agent_finish = facts.phase_finished.get("agent")
        verifier_start = facts.phase_started.get("verifier")
        verifier_finish = facts.phase_finished.get("verifier")
        if (
            agent_start is None
            or agent_finish is None
            or verifier_start is None
            or verifier_finish is None
        ):
            raise ValueError(f"machine-check trial {facts.trial} has incomplete phase times")
        if name not in digests or name not in agent_budgets or name not in verifier_budgets:
            raise ValueError(f"machine-check trial {facts.trial} has no task digest or budget")
        tasks.append(
            {
                "name": name,
                "digest": digests[name],
                "arch": arches[name],
                "memory_mb": memories[name],
                "oracle_reward": facts.reward,
                "agent_s": agent_finish - agent_start,
                "agent_budget_s": agent_budgets[name],
                "verifier_s": verifier_finish - verifier_start,
                "verifier_budget_s": verifier_budgets[name],
                "nothing_reward": nothing_rewards.get(name),
            }
        )

    if job.tasks.images == "prebuilt":
        included = set(included_tasks(tasks))
        for task in tasks:
            if task["name"] not in included and task["arch"] != docker_host_arch:
                logger.warning(
                    "task %s was excluded: image architecture %s differs from Docker host "
                    "architecture %s; set tasks.images: build if this host cannot emulate %s",
                    task["name"],
                    task["arch"],
                    docker_host_arch,
                    task["arch"],
                )

    task_source = task_source_record(job)
    check = MachineCheck(
        created_t=created_t,
        tool_version=version("harbor-research-runner"),
        harbor_version=deps.checks.harbor_version(),
        host_fingerprint=deps.checks.host_fingerprint(),
        host_profile=deps.checks.host_profile(),
        task_source=task_source,
        concurrency=job.concurrency,
        tasks=tasks,
        included=included_tasks(tasks),
        verifier_timeout_multiplier=verifier_multiplier(tasks),
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    write(out, check)
    return check
