from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable
from pathlib import Path

_UNITS = {"B": 1 / 1024**2, "KiB": 1 / 1024, "MiB": 1.0, "GiB": 1024.0}
_MEMORY = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(B|KiB|MiB|GiB)\s*/")


def parse_mem_usage(value: str) -> float:
    match = _MEMORY.match(value)
    if match is None:
        raise ValueError(f"invalid Docker memory usage {value!r}")
    return float(match.group(1)) * _UNITS[match.group(2)]


def reader_for_memory_cap(memory_cap: str) -> str:
    if memory_cap == "kernel":
        return "docker_stats"
    if memory_cap == "none":
        return "proc_pss"
    raise ValueError(f"unsupported memory cap {memory_cap!r}")


def _memory_kb(text: str, prefix: str) -> int:
    for line in text.splitlines():
        if line.startswith(prefix):
            parts = line.split()
            if len(parts) >= 3 and parts[2] == "kB":
                return int(parts[1])
            raise ValueError(f"invalid {prefix.rstrip(':')} value")
    raise ValueError(f"missing {prefix.rstrip(':')} value")


def proc_rss_mb(
    pid: int,
    *,
    read_text: Callable[[Path], str] = lambda path: path.read_text(encoding="utf-8"),
    list_dir: Callable[[Path], list[Path]] = lambda path: list(path.iterdir()),
) -> tuple[float, str]:
    pending = [pid]
    seen: set[int] = set()
    total_kb = 0
    used_rss = False
    while pending:
        current = pending.pop()
        if current in seen:
            continue
        seen.add(current)
        task_dir = Path(f"/proc/{current}/task")
        try:
            tids = [int(path.name) for path in list_dir(task_dir)]
        except OSError:
            if current == pid:
                raise
            continue
        try:
            smaps = read_text(Path(f"/proc/{current}/smaps_rollup"))
        except OSError:
            try:
                status = read_text(Path(f"/proc/{current}/status"))
            except OSError:
                if current == pid:
                    raise
                continue
            total_kb += _memory_kb(status, "VmRSS:")
            used_rss = True
        else:
            total_kb += _memory_kb(smaps, "Pss:")
        for tid in tids:
            try:
                children = read_text(Path(f"/proc/{current}/task/{tid}/children"))
            except OSError:
                continue
            pending.extend(int(child) for child in children.split())
    return total_kb / 1024, "proc_rss" if used_rss else "proc_pss"


def compose_project_label(row: dict) -> str | None:
    labels = row.get("Labels", "")
    if isinstance(labels, dict):
        value = labels.get("com.docker.compose.project")
        return value if isinstance(value, str) else None
    for label in str(labels).split(","):
        name, separator, value = label.partition("=")
        if separator and name == "com.docker.compose.project":
            return value
    return None


def _run(command: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(command, check=True, capture_output=True, text=True, encoding="utf-8")


def kill_container(container_id: str, run: Callable = _run) -> None:
    run(["docker", "kill", container_id])


def running_containers(run: Callable = _run) -> list[dict]:
    result = run(["docker", "ps", "--filter", "status=running", "--format", "{{json .}}"])
    return [json.loads(line) for line in result.stdout.splitlines() if line]


def docker_stats_memory(
    run: Callable = _run, containers: list[dict] | None = None
) -> dict[str, float]:
    stats = run(["docker", "stats", "--no-stream", "--format", "{{json .}}"])
    containers = running_containers(run) if containers is None else containers
    values: dict[str, float] = {}
    for line in stats.stdout.splitlines():
        if not line:
            continue
        row = json.loads(line)
        container = row.get("Container") or row.get("Name") or row.get("ID")
        values[str(container)] = parse_mem_usage(row["MemUsage"])
    result = {}
    for row in containers:
        container_id = str(row.get("ID", ""))
        name = str(row.get("Names", row.get("Name", ""))).lstrip("/")
        candidates = (container_id, container_id[:12], name)
        value = next((values[candidate] for candidate in candidates if candidate in values), None)
        if value is not None:
            result[container_id] = value
    return result


def proc_container_memory(
    container_id: str,
    run: Callable = _run,
    read_text=Path.read_text,
    list_dir: Callable[[Path], list[Path]] = lambda path: list(path.iterdir()),
) -> tuple[float, str]:
    result = run(["docker", "inspect", "--format", "{{json .State}}", container_id])
    state = json.loads(result.stdout)
    pid = state.get("Pid")
    if type(pid) is not int or pid <= 0:
        raise ValueError(f"container {container_id!r} has no process")
    return proc_rss_mb(pid, read_text=read_text, list_dir=list_dir)
