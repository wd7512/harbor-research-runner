from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path

from harbor_research_runner.harbor_adapter import job_folder
from harbor_research_runner.harbor_adapter.commands import (
    machine_check_command,
    oracle_job_config,
    write_job_config,
)
from harbor_research_runner.harbor_adapter.task_files import task_tomls
from harbor_research_runner.job_config.job_file import Job
from harbor_research_runner.records.machine_check import MachineCheck, write
from harbor_research_runner.run.checks import CheckDeps, machine_check_launch_checks


@dataclass(frozen=True)
class MachineCheckDeps:
    checks: CheckDeps
    run_harbor: Callable[[list[str], Path], int]
    now: Callable[[], float]


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
    write_job_config(
        check_dir / oracle_config_name,
        oracle_job_config(job, task_names, "oracle", oracle_name),
    )
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

    nothing_rewards: dict[str, float | None] = {name: None for name in oracle_trials}
    if with_do_nothing:
        nop_name = "nop-job"
        nop_config_name = "nop-job-config.yaml"
        write_job_config(
            check_dir / nop_config_name,
            oracle_job_config(job, task_names, "nop", nop_name),
        )
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
                "oracle_reward": facts.reward,
                "agent_s": agent_finish - agent_start,
                "agent_budget_s": agent_budgets[name],
                "verifier_s": verifier_finish - verifier_start,
                "verifier_budget_s": verifier_budgets[name],
                "nothing_reward": nothing_rewards.get(name),
            }
        )

    task_source = _task_source(job)
    check = MachineCheck(
        created_t=created_t,
        tool_version=version("harbor-research-runner"),
        harbor_version=deps.checks.harbor_version(),
        host_fingerprint=deps.checks.host_fingerprint(),
        task_source=task_source,
        concurrency=job.concurrency,
        tasks=tasks,
        included=included_tasks(tasks),
        verifier_timeout_multiplier=verifier_multiplier(tasks),
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    write(out, check)
    return check


def _task_source(job: Job) -> dict:
    tasks = job.tasks
    if tasks.kind == "dataset":
        return {"dataset": tasks.dataset}
    if tasks.kind == "path":
        return {"path": tasks.path}
    if tasks.kind == "git":
        return {"git": {"url": tasks.git_url, "commit": tasks.git_commit}}
    raise ValueError(f"unknown task source kind {tasks.kind!r}")
