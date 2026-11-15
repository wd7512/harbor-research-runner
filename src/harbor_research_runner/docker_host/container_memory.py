from __future__ import annotations

import json
import os
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
    readlink: Callable[[Path], str] = os.readlink,
) -> tuple[float, str, list[dict[str, int | float | str]]]:
    init_namespace = readlink(Path(f"/proc/{pid}/ns/pid"))
    if init_namespace == readlink(Path("/proc/self/ns/pid")):
        raise ValueError("container shares the host PID namespace")
    total_kb = 0
    used_rss = False
    processes = []
    for process_dir in list_dir(Path("/proc")):
        if not process_dir.name.isdecimal():
            continue
        try:
            namespace = readlink(process_dir / "ns/pid")
        except OSError:
            continue
        if namespace != init_namespace:
            continue
        try:
            smaps = read_text(process_dir / "smaps_rollup")
        except OSError:
            try:
                status = read_text(process_dir / "status")
            except OSError:
                continue
            memory_kb = _memory_kb(status, "VmRSS:")
            used_rss = True
        else:
            memory_kb = _memory_kb(smaps, "Pss:")
        total_kb += memory_kb
        try:
            command = read_text(process_dir / "cmdline").replace("\0", " ").strip()
        except OSError:
            command = ""
        processes.append({"pid": int(process_dir.name), "cmd": command, "mb": memory_kb / 1024})
    processes.sort(key=lambda process: process["mb"], reverse=True)
    return total_kb / 1024, "proc_rss" if used_rss else "proc_pss", processes


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
        used_mb = parse_mem_usage(row["MemUsage"])
        for key in ("Container", "ID", "Name"):
            if row.get(key):
                values[str(row[key])] = used_mb
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
    readlink: Callable[[Path], str] = os.readlink,
) -> tuple[float, str, list[dict[str, int | float | str]]]:
    result = run(["docker", "inspect", "--format", "{{json .State}}", container_id])
    state = json.loads(result.stdout)
    pid = state.get("Pid")
    if type(pid) is not int or pid <= 0:
        raise ValueError(f"container {container_id!r} has no process")
    return proc_rss_mb(pid, read_text=read_text, list_dir=list_dir, readlink=readlink)


def docker_top_processes(
    container_id: str, run: Callable = _run
) -> list[dict[str, int | float | str]]:
    result = run(["docker", "top", container_id, "-eo", "pid,rss,args"])
    lines = result.stdout.splitlines()
    if not lines or lines[0].split()[:3] != ["PID", "RSS", "COMMAND"]:
        raise ValueError("invalid docker top output")
    processes = []
    for line in lines[1:]:
        if not line.strip():
            continue
        parts = line.split(maxsplit=2)
        if len(parts) != 3:
            raise ValueError("invalid docker top process row")
        pid, rss = int(parts[0]), int(parts[1])
        processes.append({"pid": pid, "cmd": parts[2], "mb": rss / 1024})
    processes.sort(key=lambda process: process["mb"], reverse=True)
    return processes[:20]
