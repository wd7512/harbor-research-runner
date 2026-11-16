from __future__ import annotations

import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from importlib.metadata import version
from pathlib import Path


@dataclass(frozen=True)
class ToolIdentity:
    version: str
    commit: str


_COMMIT = re.compile(r"[0-9a-f]{40}")
_PACKAGE_DIR = Path(__file__).resolve().parent


def tool_identity(
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> ToolIdentity:
    tool_version = version("harbor-research-runner")
    command = ["git", "-C", str(_PACKAGE_DIR)]
    try:
        result = run([*command, "rev-parse", "HEAD"], capture_output=True, text=True, check=False)
    except FileNotFoundError:
        return ToolIdentity(tool_version, "none")
    if result.returncode != 0:
        if result.returncode == 128 and "not a git repository" in result.stderr.lower():
            return ToolIdentity(tool_version, "none")
        raise RuntimeError(result.stderr.strip() or "git rev-parse failed")
    commit = result.stdout.strip()
    if _COMMIT.fullmatch(commit) is None:
        raise RuntimeError("git rev-parse returned an invalid commit")
    try:
        status = run(
            [*command, "status", "--porcelain", "--untracked-files=no"],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return ToolIdentity(tool_version, "none")
    if status.returncode != 0:
        raise RuntimeError(status.stderr.strip() or "git status failed")
    if status.stdout:
        commit += "+dirty"
    return ToolIdentity(tool_version, commit)
