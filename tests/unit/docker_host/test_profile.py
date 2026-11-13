import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from harbor_research_runner.docker_host.profile import host_profile
from harbor_research_runner.docker_host.resources import host_fingerprint

FIXTURES = Path(__file__).parents[2] / "fixtures" / "docker_info"


def _run_fixture(path: Path):
    payload = path.read_text(encoding="utf-8")
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout=payload, stderr="")

    return run, calls


def test_host_profile_reads_docker_info_once_and_maps_capabilities():
    run, calls = _run_fixture(FIXTURES / "docker-desktop-macos.json")

    profile = host_profile(run=run)

    assert profile == {
        "arch": "arm64",
        "cpus": 15,
        "docker_version": "29.8.0",
        "memory_cap": "kernel",
        "swap_limit": True,
        "rootless": False,
        "cgroup_version": "2",
        "docker_memory_mb": 15969,
    }
    assert len(calls) == 1
    assert calls[0][0] == ["docker", "info", "--format", "{{json .}}"]


def test_host_profile_identifies_rootless_daemon():
    """The rootless Docker info variant is hand-written from the documented fields."""
    run, _calls = _run_fixture(FIXTURES / "rootless.json")

    profile = host_profile(run=run)

    assert profile["memory_cap"] == "none"
    assert profile["swap_limit"] is False
    assert profile["rootless"] is True


def test_host_profile_preserves_missing_cgroup_version():
    run, _calls = _run_fixture(FIXTURES / "no-cgroup.json")

    assert host_profile(run=run)["cgroup_version"] == ""


def test_host_profile_raises_when_docker_info_fails():
    def run(argv, **kwargs):
        return SimpleNamespace(returncode=1, stdout="", stderr="daemon unavailable")

    with pytest.raises(OSError, match="daemon unavailable"):
        host_profile(run=run)


def test_host_fingerprint_does_not_depend_on_hostname():
    run, _calls = _run_fixture(FIXTURES / "docker-desktop-macos.json")
    profile = host_profile(run=run)
    first_host = {"hostname": "node-a", **profile}
    second_host = {"hostname": "node-b", **profile}
    expected = hashlib.sha256(
        json.dumps(profile, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:12]

    assert host_fingerprint(profile=first_host) == expected
    assert host_fingerprint(profile=second_host) == expected
