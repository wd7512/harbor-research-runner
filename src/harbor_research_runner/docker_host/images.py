from __future__ import annotations

import logging
import subprocess
from collections.abc import Callable

logger = logging.getLogger(__name__)

ExternalRunner = Callable[..., subprocess.CompletedProcess[str]]


def _run(
    args: list[str],
    run: ExternalRunner,
    *,
    timeout: int | None = None,
) -> subprocess.CompletedProcess[str]:
    kwargs: dict[str, object] = {"capture_output": True, "text": True, "check": False}
    if timeout is not None:
        kwargs["timeout"] = timeout
    logger.debug("running: %s", " ".join(args))
    return run(args, **kwargs)


def build_argv(
    arch: str,
    node_base: str,
    commit: str,
    ref: str,
    dockerfile: str,
    context: str,
) -> list[str]:
    return [
        "docker",
        "build",
        "--platform",
        f"linux/{arch}",
        "--build-arg",
        f"NODE_BASE={node_base}",
        "--build-arg",
        f"OPENCODE_COMMIT={commit}",
        "-t",
        ref,
        "-f",
        dockerfile,
        context,
    ]


def build_image(
    arch: str,
    node_base: str,
    commit: str,
    ref: str,
    dockerfile: str,
    context: str,
    run: ExternalRunner,
) -> subprocess.CompletedProcess[str]:
    return _run(build_argv(arch, node_base, commit, ref, dockerfile, context), run)


def inspect_image(
    ref: str,
    template: str,
    run: ExternalRunner,
) -> subprocess.CompletedProcess[str]:
    return _run(["docker", "image", "inspect", "--format", template, ref], run)


def image_digest(ref: str, run: ExternalRunner) -> subprocess.CompletedProcess[str]:
    return inspect_image(ref, "{{.Id}}", run)


def host_architecture(run: ExternalRunner) -> subprocess.CompletedProcess[str]:
    return _run(["docker", "info", "--format", "{{.Architecture}}"], run)


def image_architecture(ref: str, run: ExternalRunner) -> subprocess.CompletedProcess[str]:
    return inspect_image(ref, "{{.Architecture}}", run)


def run_version(ref: str, arch: str, run: ExternalRunner) -> subprocess.CompletedProcess[str]:
    return _run(
        ["docker", "run", "--rm", "--platform", f"linux/{arch}", ref, "opencode", "--version"], run
    )


def create(
    ref: str, name: str, run: ExternalRunner, timeout: int
) -> subprocess.CompletedProcess[str]:
    return _run(["docker", "create", "--name", name, ref], run, timeout=timeout)


def copy(
    container_path: str, destination: str, run: ExternalRunner, timeout: int
) -> subprocess.CompletedProcess[str]:
    return _run(["docker", "cp", container_path, destination], run, timeout=timeout)


def remove(name: str, run: ExternalRunner, timeout: int) -> subprocess.CompletedProcess[str]:
    return _run(["docker", "rm", name], run, timeout=timeout)
