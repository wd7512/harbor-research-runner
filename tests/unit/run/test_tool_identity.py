from __future__ import annotations

import json
import subprocess
from importlib import metadata
from types import SimpleNamespace

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


def test_tool_identity_uses_direct_url_commit_outside_a_git_checkout(monkeypatch) -> None:
    direct_url = {
        "url": "https://github.com/wd7512/harbor-research-runner",
        "vcs_info": {
            "vcs": "git",
            "commit_id": "f48a0da6370e5022918440c51059ae8de2f0da3e",
            "requested_revision": "v0.1.0",
        },
    }
    distribution = SimpleNamespace(version="0.1.0", read_text=lambda name: json.dumps(direct_url))
    monkeypatch.setattr(metadata, "distribution", lambda _name: distribution)

    def run(command, **_kwargs):
        return subprocess.CompletedProcess(command, 128, "", "fatal: not a git repository")

    identity = tool_identity(run)

    assert identity.commit == direct_url["vcs_info"]["commit_id"]
    assert identity.tool_revision == "v0.1.0"


def test_tool_identity_uses_none_for_wheel_direct_url(monkeypatch) -> None:
    distribution = SimpleNamespace(
        version="0.1.0", read_text=lambda _name: json.dumps({"url": "file:///wheel"})
    )
    monkeypatch.setattr(metadata, "distribution", lambda _name: distribution)

    def run(command, **_kwargs):
        return subprocess.CompletedProcess(command, 128, "", "fatal: not a git repository")

    identity = tool_identity(run)

    assert identity.commit == "none"
    assert identity.tool_revision is None


def test_tool_identity_uses_none_for_malformed_direct_url_commit(monkeypatch) -> None:
    direct_url = {"vcs_info": {"vcs": "git", "commit_id": "bad", "requested_revision": "v0"}}
    distribution = SimpleNamespace(version="0.1.0", read_text=lambda _name: json.dumps(direct_url))
    monkeypatch.setattr(metadata, "distribution", lambda _name: distribution)

    def run(command, **_kwargs):
        return subprocess.CompletedProcess(command, 128, "", "fatal: not a git repository")

    identity = tool_identity(run)

    assert identity.commit == "none"
    assert identity.tool_revision is None


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


def test_tool_identity_prefers_direct_url_over_an_enclosing_git_repository(monkeypatch) -> None:
    direct_url = {
        "url": "https://github.com/wd7512/harbor-research-runner",
        "vcs_info": {
            "vcs": "git",
            "commit_id": "f48a0da6370e5022918440c51059ae8de2f0da3e",
            "requested_revision": "v0.1.0",
        },
    }
    distribution = SimpleNamespace(version="0.1.0", read_text=lambda name: json.dumps(direct_url))
    monkeypatch.setattr(metadata, "distribution", lambda _name: distribution)

    identity = tool_identity(_run_with_status(""))

    assert identity == ToolIdentity("0.1.0", "f48a0da6370e5022918440c51059ae8de2f0da3e", "v0.1.0")
