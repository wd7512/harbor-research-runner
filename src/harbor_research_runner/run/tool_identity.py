from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path


@dataclass(frozen=True)
class ToolIdentity:
    version: str
    commit: str
    tool_revision: str | None = None


_COMMIT = re.compile(r"[0-9a-f]{40}")
_PACKAGE_DIR = Path(__file__).resolve().parent


def tool_identity(
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> ToolIdentity:
    tool_version = metadata.version("harbor-research-runner")
    installed = _installed_identity(tool_version)
    if installed.commit != "none":
        return installed
    command = ["git", "-C", str(_PACKAGE_DIR)]
    try:
        result = run([*command, "rev-parse", "HEAD"], capture_output=True, text=True, check=False)
    except FileNotFoundError:
        return installed
    if result.returncode != 0:
        if result.returncode == 128 and "not a git repository" in result.stderr.lower():
            return installed
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


def _installed_identity(tool_version: str) -> ToolIdentity:
    try:
        direct_url = metadata.distribution("harbor-research-runner").read_text("direct_url.json")
        data = json.loads(direct_url) if direct_url is not None else None
    except (OSError, json.JSONDecodeError, metadata.PackageNotFoundError):
        data = None
    vcs_info = data.get("vcs_info") if isinstance(data, dict) else None
    commit = vcs_info.get("commit_id") if isinstance(vcs_info, dict) else None
    revision = vcs_info.get("requested_revision") if isinstance(vcs_info, dict) else None
    if not isinstance(commit, str) or _COMMIT.fullmatch(commit) is None:
        return ToolIdentity(tool_version, "none")
    return ToolIdentity(tool_version, commit, revision if isinstance(revision, str) else None)
