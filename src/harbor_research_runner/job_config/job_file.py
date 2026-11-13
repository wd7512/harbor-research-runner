from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True)
class Arm:
    label: str
    harness: str
    commit: str
    fake: dict | None = None


@dataclass(frozen=True)
class TaskSource:
    kind: str
    dataset: str | None
    path: str | None
    git_url: str | None
    git_commit: str | None
    include: tuple[str, ...]
    exclude: tuple[str, ...]


@dataclass(frozen=True)
class Job:
    path: Path
    name: str
    model: str
    repetitions: int
    concurrency: int
    task_order: str
    tasks: TaskSource
    machine_check: Path
    arms: tuple[Arm, ...]
    proxy_host: str
    proxy_port: int
    container_host: str
    limit_overrides: dict


class JobFileError(ValueError):
    pass


_TOP_LEVEL = {
    "name",
    "model",
    "repetitions",
    "concurrency",
    "task_order",
    "tasks",
    "machine_check",
    "arms",
    "proxy",
    "limits",
}
_LIMITS = {
    "tolerated_errors",
    "tolerated_error_fraction",
    "trial_output_rate_min",
    "first_chunk_max_s",
    "inter_chunk_gap_max_s",
    "request_latency_max_s",
    "halt_after_consecutive_invalid",
    "zero_billed_window_s",
    "min_spare_memory_mb",
    "memory_reserve_mb",
    "max_swapped_out_pages",
    "min_free_disk_gb",
    "host_breach_halt_s",
    "launch_min_interval_s",
    "max_set_asides_per_slot",
    "verifier_timeout_multiplier",
    "inactivity_margin_s",
}


def _mapping(value: object, name: str) -> dict:
    if not isinstance(value, dict):
        raise JobFileError(f"{name} must be a mapping")
    return value


def _keys(data: dict, allowed: set[str], name: str) -> None:
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise JobFileError(f"unknown {name} key(s): {', '.join(unknown)}")


def _required(data: dict, key: str) -> object:
    if key not in data:
        raise JobFileError(f"missing required field: {key}")
    return data[key]


def _nonempty_string(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise JobFileError(f"{name} must be a non-empty string")
    return value


def _positive_integer(value: object, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise JobFileError(f"{name} must be an integer >= 1")
    return value


def _task_source(value: object) -> TaskSource:
    data = _mapping(value, "tasks")
    _keys(data, {"dataset", "path", "git", "include", "exclude"}, "tasks")
    kinds = [key for key in ("dataset", "path", "git") if key in data]
    if len(kinds) != 1:
        raise JobFileError("tasks must specify exactly one of dataset, path, or git")
    kind = kinds[0]
    dataset = path = git_url = git_commit = None
    if kind == "dataset":
        dataset = _nonempty_string(data[kind], "tasks.dataset")
    elif kind == "path":
        path = _nonempty_string(data[kind], "tasks.path")
    else:
        git = _mapping(data[kind], "tasks.git")
        _keys(git, {"url", "commit"}, "tasks.git")
        git_url = _nonempty_string(_required(git, "url"), "tasks.git.url")
        git_commit = _nonempty_string(_required(git, "commit"), "tasks.git.commit")
        if not re.fullmatch(r"[0-9a-f]{40}", git_commit):
            raise JobFileError("tasks.git.commit must be 40 lowercase hexadecimal characters")
    lists: dict[str, tuple[str, ...]] = {}
    for key in ("include", "exclude"):
        raw = data.get(key, [])
        if not isinstance(raw, list) or any(not isinstance(item, str) for item in raw):
            raise JobFileError(f"tasks.{key} must be a list of strings")
        lists[key] = tuple(raw)
    return TaskSource(kind, dataset, path, git_url, git_commit, lists["include"], lists["exclude"])


def load_job(path: Path) -> Job:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise JobFileError(f"could not read job file {path}: {exc}") from exc
    data = _mapping(raw, "job file")
    _keys(data, _TOP_LEVEL, "job")
    name = _nonempty_string(_required(data, "name"), "name")
    model = _nonempty_string(_required(data, "model"), "model")
    repetitions = _positive_integer(_required(data, "repetitions"), "repetitions")
    concurrency = _positive_integer(_required(data, "concurrency"), "concurrency")
    task_order_value = _required(data, "task_order")
    if not isinstance(task_order_value, str) or task_order_value not in {
        "slowest_first",
        "as_listed",
    }:
        raise JobFileError("task_order must be slowest_first or as_listed")
    task_order = task_order_value
    tasks = _task_source(_required(data, "tasks"))
    machine_check = Path(_nonempty_string(_required(data, "machine_check"), "machine_check"))
    if not machine_check.is_absolute():
        machine_check = path.parent / machine_check
    arms_raw = _required(data, "arms")
    if not isinstance(arms_raw, list):
        raise JobFileError("arms must be a list")
    if not arms_raw:
        raise JobFileError("job must have at least one arm")
    arms: list[Arm] = []
    labels: set[str] = set()
    for index, raw_arm in enumerate(arms_raw):
        arm_data = _mapping(raw_arm, f"arms[{index}]")
        _keys(arm_data, {"label", "harness", "commit", "fake"}, f"arms[{index}]")
        label = _nonempty_string(_required(arm_data, "label"), f"arms[{index}].label")
        if not re.fullmatch(r"[a-z0-9][a-z0-9.-]*", label):
            raise JobFileError(f"arms[{index}].label must match [a-z0-9][a-z0-9.-]*")
        if label in labels:
            raise JobFileError(f"arm labels must be unique: {label!r}")
        labels.add(label)
        harness = _nonempty_string(_required(arm_data, "harness"), f"arms[{index}].harness")
        commit = _nonempty_string(_required(arm_data, "commit"), f"arms[{index}].commit")
        if not re.fullmatch(r"[0-9a-f]{40}", commit):
            raise JobFileError(f"arms[{index}].commit must be 40 lowercase hexadecimal characters")
        fake = None
        if "fake" in arm_data:
            if harness != "fake":
                raise JobFileError(f"arms[{index}].fake is only allowed for the fake harness")
            raw_fake = arm_data["fake"]
            if raw_fake is not None:
                fake = _mapping(raw_fake, f"arms[{index}].fake")
        arms.append(Arm(label, harness, commit, fake))
    proxy = _mapping(_required(data, "proxy"), "proxy")
    _keys(proxy, {"host", "port", "container_host"}, "proxy")
    proxy_host = _nonempty_string(_required(proxy, "host"), "proxy.host")
    proxy_port = _positive_integer(_required(proxy, "port"), "proxy.port")
    container_host = _nonempty_string(_required(proxy, "container_host"), "proxy.container_host")
    limits = _mapping(_required(data, "limits"), "limits")
    if "max_swap_used_mb" in limits:
        raise JobFileError("limits.max_swap_used_mb was replaced by max_swapped_out_pages")
    _keys(limits, _LIMITS, "limits")
    return Job(
        path=path.resolve(),
        name=name,
        model=model,
        repetitions=repetitions,
        concurrency=concurrency,
        task_order=task_order,
        tasks=tasks,
        machine_check=machine_check.resolve(),
        arms=tuple(arms),
        proxy_host=proxy_host,
        proxy_port=proxy_port,
        container_host=container_host,
        limit_overrides=limits,
    )


def planned_slots(job: Job, task_names: Sequence[str]) -> list[tuple[str, str]]:
    return [(task, arm.label) for task in task_names for arm in job.arms]
