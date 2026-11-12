from __future__ import annotations

import subprocess

import pytest

from harbor_research_runner.run.tool_identity import ToolIdentity, tool_identity

_COMMIT = "a" * 40


def _run_with_status(status: str):
    def run(command, **_kwargs):
        if command[-2:] == ["rev-parse", "HEAD"]:
            return subprocess.CompletedProcess(command, 0, _COMMIT + "\n", "")
        return subprocess.CompletedProcess(command, 0, status, "")

    return run


def test_tool_identity_uses_current_package_version_and_clean_commit() -> None:
    identity = tool_identity(_run_with_status(""))

    assert identity == ToolIdentity("0.1.0", _COMMIT)


def test_tool_identity_marks_a_dirty_checkout() -> None:
    identity = tool_identity(_run_with_status(" M src/file.py\n"))

    assert identity.commit == f"{_COMMIT}+dirty"


def test_tool_identity_uses_none_outside_a_git_checkout() -> None:
    def run(command, **_kwargs):
        return subprocess.CompletedProcess(command, 128, "", "fatal: not a git repository")

    identity = tool_identity(run)

    assert identity.commit == "none"


def test_tool_identity_uses_none_when_git_is_missing() -> None:
    def run(_command, **_kwargs):
        raise FileNotFoundError("git")

    identity = tool_identity(run)

    assert identity.commit == "none"


def test_tool_identity_raises_on_other_git_failures() -> None:
    def run(command, **_kwargs):
        return subprocess.CompletedProcess(command, 1, "", "permission denied")

    with pytest.raises(RuntimeError, match="permission denied"):
        tool_identity(run)
