from __future__ import annotations

import json
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from harbor_research_runner.trial_validity.phase_table import failed_phase
from harbor_research_runner.trial_validity.verdict import TrialFacts

_FACT_PHASES = {
    "environment_setup": "environment_setup",
    "agent_setup": "agent_setup",
    "agent_execution": "agent",
    "verifier": "verifier",
}
_PHASE_ORDER = ("environment_setup", "agent_setup", "agent", "verifier")


class HarborFolderError(ValueError):
    """A Harbor job folder cannot be read consistently."""


@dataclass(frozen=True)
class TrialDetails:
    turns: int | None
    tool_calls: int | None
    agent_wall_s: float | None
    harbor_exception: str | None
    agent_exit_nonzero: bool
    harness_commit: str | None


def trial_folders(job_dir: Path) -> list[Path]:
    """Return trial directories with a config file, ordered by folder name."""
    try:
        folders = [
            child
            for child in job_dir.iterdir()
            if child.is_dir() and (child / "config.json").is_file()
        ]
    except OSError:
        return []
    return sorted(folders, key=lambda path: path.name)


def read_trial(trial_dir: Path) -> TrialFacts:
    """Read Harbor-owned trial output into the facts used by trial validity."""
    config = _read_config(trial_dir)
    task, arm = _task_and_arm(config, trial_dir)
    result = _read_result(trial_dir)
    trial = _trial_name(config, result, trial_dir)
    if result is not None:
        result_task = result.get("task_name")
        if not isinstance(result_task, str) or result_task != task:
            raise HarborFolderError(f"{trial_dir.name}: task mismatch in result.json")
        if result.get("step_results"):
            raise HarborFolderError(f"{trial_dir.name}: unsupported step_results in result.json")
    if result is None or "agent_execution" not in result or "exception_info" not in result:
        return _empty_facts(trial, task, arm)
    phase_started, phase_finished = _phase_times(result)
    return TrialFacts(
        trial=trial,
        task=task,
        arm=arm,
        has_result=True,
        reward=_reward(result),
        exception_type=_exception_type(result),
        phase_started=phase_started,
        phase_finished=phase_finished,
        harbor_reported_tokens=_reported_tokens(result),
    )


def read_trial_details(trial_dir: Path) -> TrialDetails:
    """Read trajectory metrics and harness details for one Harbor trial."""
    config = _read_config(trial_dir)
    _, _ = _task_and_arm(config, trial_dir)
    kwargs = _agent_kwargs(config, trial_dir)
    commit = _commit_setting(kwargs, trial_dir)
    facts = read_trial(trial_dir)
    result = _read_result(trial_dir)
    trajectory = _read_trajectory(trial_dir)
    turns, tool_calls = _trajectory_details(trajectory)
    execution = result.get("agent_execution") if result is not None else None
    execution = execution if isinstance(execution, dict) else {}
    started = _timestamp(execution.get("started_at"))
    finished = _timestamp(execution.get("finished_at"))
    agent_wall_s = finished - started if started is not None and finished is not None else None
    exception = _exception_type(result) if result is not None else None
    agent_exit_nonzero = (
        exception is not None
        and exception != "AgentTimeoutError"
        and failed_phase(facts) == "agent"
    )
    return TrialDetails(
        turns=turns,
        tool_calls=tool_calls,
        agent_wall_s=agent_wall_s,
        harbor_exception=exception,
        agent_exit_nonzero=agent_exit_nonzero,
        harness_commit=commit,
    )


def task_budgets(job_dir: Path, task_tomls: Mapping[str, str]) -> dict[str, tuple[float, float]]:
    """Return effective per-task agent and verifier budgets in seconds."""
    budgets: dict[str, tuple[float, float]] = {}
    for trial_dir in trial_folders(job_dir):
        config = _read_config(trial_dir)
        task = _task_name(config, trial_dir)
        try:
            task_toml = task_tomls[task]
        except KeyError as exc:
            raise HarborFolderError(
                f"{trial_dir.name}: no task.toml supplied for {task!r}"
            ) from exc
        task_agent, task_verifier = _task_timeouts(task, task_toml, trial_dir)
        agent = config.get("agent")
        agent = agent if isinstance(agent, dict) else {}
        override = agent.get("override_timeout_sec")
        agent_timeout = task_agent if override is None else _timeout_value(override, trial_dir)
        lock = _read_lock(trial_dir)
        multiplier = lock.get("timeout_multiplier", 1.0)
        multiplier_value = _timeout_value(multiplier, trial_dir)
        trial_budget = (agent_timeout * multiplier_value, task_verifier * multiplier_value)
        previous = budgets.get(task)
        if previous is not None and previous != trial_budget:
            raise HarborFolderError(f"{trial_dir.name}: conflicting task budget for {task!r}")
        budgets[task] = trial_budget
    return budgets


def task_digests(job_dir: Path) -> dict[str, str]:
    """Read task digests from trial lock files, refusing inconsistent pins."""
    digests: dict[str, str] = {}
    for trial_dir in trial_folders(job_dir):
        lock = _read_lock(trial_dir)
        task = lock.get("task")
        task = task if isinstance(task, dict) else {}
        name = task.get("name")
        digest = task.get("digest")
        if not isinstance(name, str) or not isinstance(digest, str):
            raise HarborFolderError(f"{trial_dir.name}: missing task name or digest in lock.json")
        previous = digests.get(name)
        if previous is not None and previous != digest:
            raise HarborFolderError(f"{trial_dir.name}: conflicting task digest for {name!r}")
        digests[name] = digest
    return digests


def _read_config(trial_dir: Path) -> dict[str, Any]:
    path = trial_dir / "config.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise HarborFolderError(f"{trial_dir.name}: unreadable {path.name}") from exc
    if not isinstance(payload, dict):
        raise HarborFolderError(f"{trial_dir.name}: invalid {path.name}")
    return payload


def _read_result(trial_dir: Path) -> dict[str, Any] | None:
    path = trial_dir / "result.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _read_lock(trial_dir: Path) -> dict[str, Any]:
    path = trial_dir / "lock.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise HarborFolderError(f"{trial_dir.name}: unreadable {path.name}") from exc
    if not isinstance(payload, dict):
        raise HarborFolderError(f"{trial_dir.name}: invalid {path.name}")
    return payload


def _task_name(config: Mapping[str, Any], trial_dir: Path) -> str:
    task = config.get("task")
    task = task if isinstance(task, dict) else {}
    name = task.get("name")
    if not isinstance(name, str):
        raise HarborFolderError(f"{trial_dir.name}: missing task name in config.json")
    return name


def _agent_kwargs(config: Mapping[str, Any], trial_dir: Path) -> dict[str, Any]:
    agent = config.get("agent")
    agent = agent if isinstance(agent, dict) else {}
    kwargs = agent.get("kwargs")
    if not isinstance(kwargs, dict):
        raise HarborFolderError(f"{trial_dir.name}: missing agent settings in config.json")
    return kwargs


def _task_and_arm(config: Mapping[str, Any], trial_dir: Path) -> tuple[str, str]:
    task = _task_name(config, trial_dir)
    kwargs = _agent_kwargs(config, trial_dir)
    arm = kwargs.get("arm_label")
    if not isinstance(arm, str):
        raise HarborFolderError(f"{trial_dir.name}: missing string arm_label in config.json")
    return task, arm


def _commit_setting(kwargs: Mapping[str, Any], trial_dir: Path) -> str | None:
    if "commit" not in kwargs:
        raise HarborFolderError(f"{trial_dir.name}: missing commit in config.json")
    commit = kwargs["commit"]
    if commit is not None and not isinstance(commit, str):
        raise HarborFolderError(f"{trial_dir.name}: invalid commit in config.json")
    return commit


def _trial_name(
    config: Mapping[str, Any], result: Mapping[str, Any] | None, trial_dir: Path
) -> str:
    if result is not None and isinstance(result.get("trial_name"), str):
        return result["trial_name"]
    config_name = config.get("trial_name")
    return config_name if isinstance(config_name, str) else trial_dir.name


def _empty_facts(trial: str, task: str, arm: str) -> TrialFacts:
    empty_phases: dict[str, float | None] = {phase: None for phase in _PHASE_ORDER}
    return TrialFacts(
        trial=trial,
        task=task,
        arm=arm,
        has_result=False,
        reward=None,
        exception_type=None,
        phase_started=empty_phases.copy(),
        phase_finished=empty_phases.copy(),
        harbor_reported_tokens=0,
    )


def _phase_times(
    result: Mapping[str, Any],
) -> tuple[dict[str, float | None], dict[str, float | None]]:
    started: dict[str, float | None] = {phase: None for phase in _PHASE_ORDER}
    finished: dict[str, float | None] = {phase: None for phase in _PHASE_ORDER}
    for source, target in _FACT_PHASES.items():
        timing = result.get(source)
        if not isinstance(timing, dict):
            continue
        started[target] = _timestamp(timing.get("started_at"))
        finished[target] = _timestamp(timing.get("finished_at"))
    return started, finished


def _timestamp(value: object) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (OverflowError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.timestamp()


def _reward(result: Mapping[str, Any]) -> float | None:
    verifier_result = result.get("verifier_result")
    verifier_result = verifier_result if isinstance(verifier_result, dict) else {}
    rewards = verifier_result.get("rewards")
    rewards = rewards if isinstance(rewards, dict) else {}
    reward = rewards.get("reward")
    if reward is None and len(rewards) == 1:
        reward = next(iter(rewards.values()))
    if isinstance(reward, bool) or reward is None:
        return None
    try:
        return float(reward)
    except (TypeError, ValueError):
        return None


def _exception_type(result: Mapping[str, Any]) -> str | None:
    info = result.get("exception_info")
    info = info if isinstance(info, dict) else {}
    exception = info.get("exception_type")
    return exception if isinstance(exception, str) and exception else None


def _reported_tokens(result: Mapping[str, Any]) -> int:
    agent_result = result.get("agent_result")
    agent_result = agent_result if isinstance(agent_result, dict) else {}
    input_tokens = agent_result.get("n_input_tokens")
    output_tokens = agent_result.get("n_output_tokens")
    input_count = input_tokens if type(input_tokens) is int and input_tokens >= 0 else 0
    output_count = output_tokens if type(output_tokens) is int and output_tokens >= 0 else 0
    return input_count + output_count


def _read_trajectory(trial_dir: Path) -> dict[str, Any] | None:
    path = trial_dir / "agent" / "trajectory.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _trajectory_details(trajectory: Mapping[str, Any] | None) -> tuple[int | None, int | None]:
    if trajectory is None:
        return None, None
    steps = trajectory.get("steps")
    final_metrics = trajectory.get("final_metrics")
    final_metrics = final_metrics if isinstance(final_metrics, dict) else {}
    total_steps = final_metrics.get("total_steps")
    if type(total_steps) is int:
        turns = total_steps
    elif isinstance(steps, list):
        turns = len(steps)
    else:
        turns = None
    if not isinstance(steps, list):
        return turns, None
    calls = 0
    for step in steps:
        if not isinstance(step, dict):
            continue
        tool_calls = step.get("tool_calls")
        if not isinstance(tool_calls, list):
            continue
        calls += sum(
            1
            for call in tool_calls
            if isinstance(call, dict) and isinstance(call.get("function_name"), str)
        )
    return turns, calls or None


def _task_timeouts(task: str, text: str, trial_dir: Path) -> tuple[float, float]:
    try:
        task_config = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise HarborFolderError(f"{trial_dir.name}: invalid task.toml for {task!r}") from exc
    agent = task_config.get("agent")
    verifier = task_config.get("verifier")
    if not isinstance(agent, dict) or "timeout_sec" not in agent:
        raise HarborFolderError(f"{trial_dir.name}: missing agent timeout_sec in task.toml")
    if not isinstance(verifier, dict) or "timeout_sec" not in verifier:
        raise HarborFolderError(f"{trial_dir.name}: missing verifier timeout_sec in task.toml")
    return (
        _timeout_value(agent["timeout_sec"], trial_dir),
        _timeout_value(verifier["timeout_sec"], trial_dir),
    )


def _timeout_value(value: object, trial_dir: Path) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise HarborFolderError(f"{trial_dir.name}: invalid timeout_sec or multiplier")
    return float(value)
