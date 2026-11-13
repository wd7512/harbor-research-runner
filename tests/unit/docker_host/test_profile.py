import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from harbor_research_runner.docker_host.profile import (
    canonical_profile,
    host_profile,
    profile_differences,
)
from harbor_research_runner.docker_host.resources import host_fingerprint

FIXTURES = Path(__file__).parents[2] / "fixtures" / "docker_info"
NETWORK_FIXTURES = Path(__file__).parents[2] / "fixtures" / "docker_network"


def _run_fixture(path: Path):
    payload = path.read_text(encoding="utf-8")
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        output = (
            payload
            if argv[:2] == ["docker", "info"]
            else (NETWORK_FIXTURES / "docker-network-ls-desktop.jsonl").read_text(encoding="utf-8")
        )
        return SimpleNamespace(returncode=0, stdout=output, stderr="")

    return run, calls


def test_host_profile_reads_docker_capabilities():
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
        "container_network": "bridge",
    }
    assert len(calls) == 2
    assert calls[0][0] == ["docker", "info", "--format", "{{json .}}"]
    assert calls[1][0] == ["docker", "network", "ls", "--format", "{{json .}}"]


def test_host_profile_detects_bridge_network_from_fixture():
    info = (FIXTURES / "docker-desktop-macos.json").read_text(encoding="utf-8")
    networks = (
        Path(__file__).parents[2]
        / "fixtures"
        / "docker_network"
        / "docker-network-ls-desktop.jsonl"
    ).read_text(encoding="utf-8")
    calls = iter([info, networks])

    profile = host_profile(
        run=lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout=next(calls), stderr="")
    )

    assert profile["container_network"] == "bridge"


def test_host_profile_detects_missing_bridge_network_from_fixture():
    info = (FIXTURES / "docker-desktop-macos.json").read_text(encoding="utf-8")
    networks = (
        Path(__file__).parents[2] / "fixtures" / "docker_network" / "no-bridge.jsonl"
    ).read_text(encoding="utf-8")

    def run(argv, **_kwargs):
        return SimpleNamespace(
            returncode=0, stdout=info if argv[:2] == ["docker", "info"] else networks, stderr=""
        )

    profile = host_profile(run=run)

    assert profile["container_network"] == "host"


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
        json.dumps(canonical_profile(profile), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:12]

    assert host_fingerprint(profile=first_host) == expected
    assert host_fingerprint(profile=second_host) == expected


def test_bridge_network_profile_keeps_old_fingerprint_and_has_no_difference():
    recorded = {"arch": "arm64", "memory_cap": "kernel"}
    current = {**recorded, "container_network": "bridge"}

    assert host_fingerprint(profile=recorded) == host_fingerprint(profile=current)
    assert profile_differences(recorded, current) == []
    assert canonical_profile(current) == recorded


def test_host_network_profile_differs_from_old_profile():
    recorded = {"arch": "arm64", "memory_cap": "kernel"}
    current = {**recorded, "container_network": "host"}

    assert profile_differences(recorded, current)
