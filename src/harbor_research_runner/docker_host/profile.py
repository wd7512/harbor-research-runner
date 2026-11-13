from __future__ import annotations

import json
import subprocess
from collections.abc import Callable

from harbor_research_runner.docker_host.cleanup import DOCKER_TIMEOUT_S

PROFILE_FIELDS = (
    "arch",
    "cpus",
    "docker_version",
    "memory_cap",
    "swap_limit",
    "rootless",
    "cgroup_version",
    "docker_memory_mb",
)


def host_profile(run: Callable = subprocess.run) -> dict:
    result = run(
        ["docker", "info", "--format", "{{json .}}"],
        capture_output=True,
        text=True,
        timeout=DOCKER_TIMEOUT_S,
    )
    if result.returncode != 0:
        raise OSError(f"docker info exited {result.returncode}: {result.stderr.strip()}")
    try:
        info = json.loads(result.stdout)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValueError("Docker returned invalid info JSON") from exc
    if not isinstance(info, dict):
        raise ValueError("Docker info must be a JSON object")
    required = (
        "Architecture",
        "MemoryLimit",
        "SwapLimit",
        "SecurityOptions",
        "CgroupVersion",
        "MemTotal",
        "NCPU",
        "ServerVersion",
    )
    missing = [key for key in required if key not in info]
    if missing:
        raise ValueError(f"Docker info is missing {', '.join(missing)}")
    memory_bytes = info["MemTotal"]
    if not isinstance(memory_bytes, int) or isinstance(memory_bytes, bool) or memory_bytes < 0:
        raise ValueError("Docker returned an invalid MemTotal value")
    architecture = info["Architecture"].lower()
    arch = {"x86_64": "amd64", "aarch64": "arm64"}.get(architecture, architecture)
    if arch not in {"amd64", "arm64"}:
        raise ValueError(f"Docker returned an unsupported architecture {architecture!r}")
    cpus = info["NCPU"]
    if not isinstance(cpus, int) or isinstance(cpus, bool) or cpus < 1:
        raise ValueError("Docker returned an invalid NCPU value")
    if not isinstance(info["MemoryLimit"], bool) or not isinstance(info["SwapLimit"], bool):
        raise ValueError("Docker returned invalid memory capability flags")
    if not isinstance(info["ServerVersion"], str):
        raise ValueError("Docker returned an invalid server version")
    if not isinstance(info["CgroupVersion"], str):
        raise ValueError("Docker returned an invalid cgroup version")
    security_options = info["SecurityOptions"] or []
    if not isinstance(security_options, list):
        raise ValueError("Docker returned invalid security options")
    return {
        "arch": arch,
        "cpus": cpus,
        "docker_version": info["ServerVersion"],
        "memory_cap": "kernel" if info["MemoryLimit"] is True else "none",
        "swap_limit": info["SwapLimit"],
        "rootless": "name=rootless" in security_options,
        "cgroup_version": info["CgroupVersion"],
        "docker_memory_mb": memory_bytes // (1024**2),
    }
