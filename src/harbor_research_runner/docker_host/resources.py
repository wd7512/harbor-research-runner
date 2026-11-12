from __future__ import annotations

import hashlib
import os
import platform
import re
import shutil
import socket
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

from harbor_research_runner.docker_host.cleanup import is_residue_container
from harbor_research_runner.records.resource_stats import Sample

LINUX_MEMINFO = Path("/proc/meminfo")
DOCKER_TIMEOUT_S = 5
_MACOS_PAGE_SIZE = re.compile(r"page size of (\d+) bytes")
_MACOS_SPARE_BUCKETS = {
    "Pages free",
    "Pages inactive",
    "Pages speculative",
    "Pages purgeable",
}


def _run_result(run: Callable, argv: list[str], timeout: float = 5) -> subprocess.CompletedProcess:
    return run(argv, capture_output=True, text=True, timeout=timeout)


def _parse_memavailable(text: str) -> int:
    for line in text.splitlines():
        name, separator, rest = line.partition(":")
        fields = rest.split()
        if not separator or name.strip() != "MemAvailable":
            continue
        if not fields:
            raise ValueError("unparseable MemAvailable value")
        try:
            value = int(fields[0])
        except ValueError as exc:
            raise ValueError(f"unparseable MemAvailable value {fields[0]!r}") from exc
        if value < 0:
            raise ValueError(f"negative MemAvailable value {value}")
        return value // 1024
    raise ValueError("MemAvailable is missing from /proc/meminfo")


def _parse_meminfo(text: str) -> tuple[int, int]:
    values: dict[str, int] = {}
    for line in text.splitlines():
        name, separator, rest = line.partition(":")
        fields = rest.split()
        if separator and fields and name in {"SwapTotal", "SwapFree"}:
            values[name] = int(fields[0])
    return _parse_memavailable(text), (values["SwapTotal"] - values["SwapFree"]) // 1024


def _parse_vm_stat(text: str, page_size: int | None = None) -> int:
    if page_size is None:
        match = _MACOS_PAGE_SIZE.search(text)
        if match is None:
            raise ValueError("vm_stat header carries no page size")
        page_size = int(match.group(1))
    if page_size <= 0:
        raise ValueError(f"invalid vm_stat page size {page_size}")
    pages: dict[str, int] = {}
    for line in text.splitlines():
        name, separator, rest = line.partition(":")
        name = name.strip()
        if separator and name in _MACOS_SPARE_BUCKETS:
            value_text = rest.strip().rstrip(".").strip()
            try:
                value = int(value_text)
            except ValueError as exc:
                raise ValueError(f"unparseable vm_stat {name} value {value_text!r}") from exc
            if value < 0:
                raise ValueError(f"negative vm_stat {name} value {value}")
            pages[name] = value
    if "Pages free" not in pages:
        raise ValueError("Pages free is missing from vm_stat")
    return sum(pages.values()) * page_size // (1024 * 1024)


def spare_memory_mb(
    run: Callable = subprocess.run,
    read_text: Callable = Path.read_text,
) -> int:
    """Read available host memory in MiB without measuring swap."""
    try:
        text = read_text(LINUX_MEMINFO, encoding="utf-8")
    except FileNotFoundError:
        text = None
    if text is not None:
        return _parse_memavailable(text)

    try:
        vm_stat = _run_result(run, ["vm_stat"])
    except (OSError, subprocess.SubprocessError) as exc:
        raise OSError(f"cannot run vm_stat: {exc}") from exc
    if vm_stat.returncode != 0:
        raise OSError(f"vm_stat exited {vm_stat.returncode}: {vm_stat.stderr.strip()}")
    return _parse_vm_stat(vm_stat.stdout)


def _read_host_memory(run: Callable, read_text: Callable) -> tuple[int, int]:
    try:
        text = read_text(LINUX_MEMINFO, encoding="utf-8")
    except FileNotFoundError:
        text = None
    if text is not None:
        return _parse_meminfo(text)

    vm_stat = _run_result(run, ["vm_stat"])
    if vm_stat.returncode != 0:
        raise OSError(f"vm_stat exited {vm_stat.returncode}: {vm_stat.stderr.strip()}")
    mem_available = _parse_vm_stat(vm_stat.stdout)
    swap = _run_result(run, ["sysctl", "vm.swapusage"])
    if swap.returncode != 0:
        raise OSError(f"sysctl vm.swapusage exited {swap.returncode}: {swap.stderr.strip()}")
    import re

    match = re.search(r"\bused\s*=\s*([\d.]+)M\b", swap.stdout)
    if match is None:
        raise ValueError("sysctl vm.swapusage has no used value")
    return mem_available, int(float(match.group(1)))


def measure(
    run: Callable = subprocess.run,
    read_text: Callable = Path.read_text,
    disk_path: Path = Path("."),
) -> Sample:
    mem_available_mb, swap_used_mb = _read_host_memory(run, read_text)
    disk_free_gb = shutil.disk_usage(disk_path).free / (1024**3)
    load1 = os.getloadavg()[0]
    try:
        info = _run_result(run, ["docker", "info"], timeout=DOCKER_TIMEOUT_S)
        docker_ok = info.returncode == 0
    except (OSError, subprocess.SubprocessError):
        docker_ok = False
    trial_containers = 0
    if docker_ok:
        try:
            containers = _run_result(run, ["docker", "ps", "--format", "{{.Names}}"])
            if containers.returncode != 0:
                docker_ok = False
            else:
                trial_containers = sum(
                    is_residue_container(name.strip())
                    for name in containers.stdout.splitlines()
                    if name.strip()
                )
        except (OSError, subprocess.SubprocessError):
            docker_ok = False
    return Sample(
        t=time.time(),
        mem_available_mb=mem_available_mb,
        swap_used_mb=swap_used_mb,
        disk_free_gb=disk_free_gb,
        load1=load1,
        trial_containers=trial_containers,
        docker_ok=docker_ok,
    )


def _fingerprint_parts(run: Callable) -> tuple[str, str, str, str]:
    try:
        hostname = socket.gethostname()
    except OSError:
        hostname = ""
    try:
        arch = platform.machine()
    except OSError:
        arch = ""
    try:
        cpus = str(os.cpu_count() or "")
    except OSError:
        cpus = ""
    try:
        result = _run_result(run, ["docker", "version", "--format", "{{.Server.Version}}"], 10)
        docker_version = result.stdout.strip() if result.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        docker_version = ""
    return hostname, arch, cpus, docker_version


def host_fingerprint(run: Callable = subprocess.run) -> str:
    material = "|".join(_fingerprint_parts(run))
    return hashlib.sha256(material.encode()).hexdigest()[:12]


def host_description() -> dict:
    try:
        memory_mb = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") // (1024**2)
    except (AttributeError, OSError, ValueError):
        memory_mb = 0
    _, arch, cpus, docker_version = _fingerprint_parts(subprocess.run)
    return {
        "arch": arch,
        "cpus": int(cpus) if cpus else 0,
        "memory_mb": memory_mb,
        "docker_version": docker_version,
    }
