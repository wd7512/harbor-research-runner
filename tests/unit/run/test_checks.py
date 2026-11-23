from __future__ import annotations

import errno
import shutil
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from harbor.publisher.packager import Packager

from harbor_research_runner.docker_host.cleanup import Inventory
from harbor_research_runner.docker_host.host_lock import HostLock
from harbor_research_runner.docker_host.resources import measure as measure_host
from harbor_research_runner.harness.manifest import ImageEntry
from harbor_research_runner.job_config.job_file import Arm, Job, TaskSource
from harbor_research_runner.job_config.model_cards import ModelCard
from harbor_research_runner.model_proxy.adapters import select_adapter
from harbor_research_runner.records.machine_check import MachineCheck
from harbor_research_runner.records.resource_stats import Sample
from harbor_research_runner.run.checks import (
    CheckDeps,
    CheckResult,
    host_resources_check,
    machine_check_launch_checks,
    proxy_port_free_check,
    resolve_tasks,
    run_checks,
    task_source_record,
)

_COMMIT = "a" * 40
_DIGEST = "sha256:" + "b" * 64
_TASK_SOURCE = {"dataset": "bench/tasks"}
_HOST_PROFILE = {
    "arch": "arm64",
    "cpus": 8,
    "docker_version": "27.0.0",
    "memory_cap": "kernel",
    "swap_limit": True,
    "rootless": False,
    "cgroup_version": "2",
    "docker_memory_mb": 32768,
    "container_network": "bridge",
}


def _job(
    *,
    include: tuple[str, ...] = (),
    exclude: tuple[str, ...] = (),
    concurrency: int = 2,
    harness: str = "fake",
    commit: str = _COMMIT,
) -> Job:
    return Job(
        path=Path("job.yaml"),
        name="study",
        model="provider/model",
        repetitions=1,
        concurrency=concurrency,
        task_order="as_listed",
        tasks=TaskSource("dataset", "bench/tasks", None, None, None, include, exclude),
        machine_check=Path("machine-check.json"),
        arms=(Arm("arm-a", harness, commit),),
        proxy_host="127.0.0.1",
        proxy_port=8766,
        container_host="host.docker.internal",
        limit_overrides={},
    )


def _card(*, api: str = "chat", withdrawn: bool = False) -> ModelCard:
    return ModelCard(
        id="provider/model",
        upstream_model="upstream-model",
        api=api,
        endpoints=["https://model.invalid/v1"],
        key_env=["MODEL_KEY"],
        tokens_per_minute=None,
        pricing={"input": 0, "output": 0, "cached_read": 0},
        variants={},
        withdrawn=withdrawn,
        reason="withdrawn for testing" if withdrawn else None,
    )


def _machine_check(
    *,
    task_source: dict | None = None,
    harbor_version: str = "0.23.0",
    fingerprint: str = "fingerprint",
    host_profile: dict | None = _HOST_PROFILE,
    concurrency: int = 4,
    included: list[str] | None = None,
    tasks: list[dict] | None = None,
) -> MachineCheck:
    return MachineCheck(
        created_t=1.0,
        tool_version="1.0.0",
        harbor_version=harbor_version,
        host_fingerprint=fingerprint,
        host_profile=host_profile,
        task_source=_TASK_SOURCE if task_source is None else task_source,
        concurrency=concurrency,
        tasks=(
            [
                {"name": "task-a", "memory_mb": 512},
                {"name": "task-b", "memory_mb": 512},
            ]
            if tasks is None
            else tasks
        ),
        included=["task-a", "task-b"] if included is None else included,
        verifier_timeout_multiplier=1.0,
    )


def _sample(
    *,
    mem_available_mb: int = 4096,
    swapped_out_pages: int = 0,
    disk_free_gb: float = 100,
    docker_ok: bool = True,
) -> Sample:
    return Sample(
        t=1.0,
        mem_available_mb=mem_available_mb,
        swapped_out_pages=swapped_out_pages,
        disk_free_gb=disk_free_gb,
        load1=0.0,
        trial_containers=0,
        docker_ok=docker_ok,
    )


def _deps(
    tmp_path: Path,
    *,
    measure=lambda **_kwargs: (_sample(), 0),
    inventory=lambda: Inventory(True, "ok", (), (), (), ()),
    live_run=lambda _runs_dir: None,
    host_lock: HostLock | None = None,
    read_machine_check=lambda _path: _machine_check(),
    fingerprint: str = "fingerprint",
    profile: dict = _HOST_PROFILE,
    harbor_version: str = "0.23.0",
    read_docker_memory=lambda: 8123,
    host_memory_mb=lambda: 65536,
    bind_port=lambda _host, _port: None,
    processes=lambda _run_dir: [],
    digest_of=lambda _ref: _DIGEST,
) -> CheckDeps:
    return CheckDeps(
        select_adapter=select_adapter,
        measure=measure,
        inventory=inventory,
        live_run=live_run,
        host_lock=host_lock or HostLock(tmp_path / "host.lock"),
        read_machine_check=read_machine_check,
        host_fingerprint=lambda: fingerprint,
        host_profile=lambda: profile,
        harbor_version=lambda: harbor_version,
        docker_memory_mb=read_docker_memory,
        host_memory_mb=host_memory_mb,
        bind_port=bind_port,
        processes=processes,
        digest_of=digest_of,
        children_resolve=lambda: (True, "Python child and Harbor executable resolve"),
    )


def _run(
    tmp_path: Path,
    *,
    job: Job | None = None,
    card: ModelCard | None = None,
    manifest: dict[tuple[str, str, str], ImageEntry] | None = None,
    deps: CheckDeps | None = None,
    environment: dict[str, str] | None = None,
    run_dir: Path | None = None,
    keep_lock: bool = False,
    arch: str = "amd64",
) -> list[CheckResult]:
    selected_deps = deps or _deps(tmp_path)
    results = run_checks(
        job or _job(),
        card or _card(),
        {} if manifest is None else manifest,
        arch,
        tmp_path / "runs",
        {"MODEL_KEY": "configured"} if environment is None else environment,
        selected_deps,
        run_dir=run_dir,
    )
    if selected_deps.host_lock.held and not keep_lock:
        selected_deps.host_lock.release()
    return results


def _by_name(results: list[CheckResult], name: str) -> CheckResult:
    return next(result for result in results if result.name == name)


def test_model_card_check_fails_when_withdrawn(tmp_path: Path) -> None:
    result = _by_name(_run(tmp_path, card=_card(withdrawn=True)), "model_card")

    assert not result.ok
    assert "withdrawn" in result.detail


def test_model_card_check_passes_for_available_card(tmp_path: Path) -> None:
    result = _by_name(_run(tmp_path), "model_card")

    assert result.ok


def test_prebuilt_dataset_task_source_record_omits_image_mode() -> None:
    assert task_source_record(_job()) == {"dataset": "bench/tasks"}


def test_built_dataset_task_source_record_includes_image_mode() -> None:
    job = replace(
        _job(),
        tasks=TaskSource("dataset", "bench/tasks", None, None, None, (), (), "build"),
    )

    assert task_source_record(job) == {"dataset": "bench/tasks", "images": "build"}


def test_git_task_source_record_includes_url_and_commit() -> None:
    job = replace(
        _job(),
        tasks=TaskSource("git", None, None, "https://example.invalid/tasks.git", "abc123", (), ()),
    )

    assert task_source_record(job) == {
        "git": {"url": "https://example.invalid/tasks.git", "commit": "abc123"}
    }


def test_keys_present_check_names_missing_variable(tmp_path: Path) -> None:
    results = _run(
        tmp_path,
        environment={"MODEL_KEY": ""},
    )
    result = _by_name(results, "keys_present")

    assert not result.ok
    assert "MODEL_KEY" in result.detail
    assert "configured" not in result.detail


def test_keys_present_check_passes_when_all_variables_are_set(tmp_path: Path) -> None:
    result = _by_name(_run(tmp_path), "keys_present")

    assert result.ok


def test_memory_fits_fails_when_concurrent_task_memory_exceeds_docker_memory(
    tmp_path: Path,
) -> None:
    machine_check = _machine_check(
        tasks=[{"name": "task-a", "memory_mb": 8192}],
        included=["task-a"],
    )
    deps = _deps(
        tmp_path,
        read_machine_check=lambda _path: machine_check,
    )

    result = _by_name(_run(tmp_path, job=_job(concurrency=4), deps=deps), "memory_fits")

    assert not result.ok
    assert "concurrency=4" in result.detail
    assert "max_task_memory_mb=8192" in result.detail
    assert "memory_reserve_mb=4096" in result.detail
    assert "docker_memory_mb=8123" in result.detail


def test_memory_fits_passes_when_concurrent_task_memory_fits_docker_memory(
    tmp_path: Path,
) -> None:
    machine_check = _machine_check(
        tasks=[{"name": "task-a", "memory_mb": 2048}],
        included=["task-a"],
    )
    deps = _deps(
        tmp_path,
        read_machine_check=lambda _path: machine_check,
        read_docker_memory=lambda: 8192,
    )

    result = _by_name(_run(tmp_path, job=_job(concurrency=2), deps=deps), "memory_fits")

    assert result.ok


def test_memory_fits_ignores_concurrency_when_one_trial_fits(tmp_path: Path) -> None:
    machine_check = _machine_check(
        tasks=[{"name": "task-a", "memory_mb": 8192}],
        included=["task-a"],
    )
    deps = _deps(
        tmp_path,
        read_machine_check=lambda _path: machine_check,
        read_docker_memory=lambda: 16384,
    )
    job = replace(_job(concurrency=4), limit_overrides={"memory_reserve_mb": 4096})

    result = _by_name(_run(tmp_path, job=job, deps=deps), "memory_fits")

    assert result.ok


def test_memory_fits_accepts_watchdog_enforced_host_without_kernel_memory_cap(
    tmp_path: Path,
) -> None:
    deps = _deps(tmp_path, profile={**_HOST_PROFILE, "memory_cap": "none"})

    result = _by_name(_run(tmp_path, deps=deps), "memory_fits")

    assert result.ok


def test_host_headroom_fails_when_i5_vm_leaves_less_than_reserve(tmp_path: Path) -> None:
    profile = {**_HOST_PROFILE, "docker_memory_mb": 24029}
    deps = _deps(tmp_path, profile=profile, host_memory_mb=lambda: 32768)

    result = _by_name(_run(tmp_path, deps=deps), "host_headroom")

    assert not result.ok
    assert "headroom_mb=8739" in result.detail
    assert "host_reserve_mb=12288" in result.detail


def test_host_headroom_passes_with_expected_vm_headroom(tmp_path: Path) -> None:
    profile = {**_HOST_PROFILE, "docker_memory_mb": 16384}
    deps = _deps(tmp_path, profile=profile, host_memory_mb=lambda: 32768)

    result = _by_name(_run(tmp_path, deps=deps), "host_headroom")

    assert result.ok
    assert "headroom_mb=16384" in result.detail


def test_host_headroom_passes_when_docker_memory_matches_host(tmp_path: Path) -> None:
    profile = {**_HOST_PROFILE, "docker_memory_mb": 32768}
    deps = _deps(tmp_path, profile=profile, host_memory_mb=lambda: 32768)

    result = _by_name(_run(tmp_path, deps=deps), "host_headroom")

    assert result.ok
    assert result.detail == "docker memory equals host memory (no VM)"


def test_host_headroom_fails_when_host_memory_is_unknown(tmp_path: Path) -> None:
    deps = _deps(tmp_path, host_memory_mb=lambda: 0)

    result = _by_name(_run(tmp_path, deps=deps), "host_headroom")

    assert not result.ok
    assert result.detail == "host memory unknown"


def test_host_network_refuses_unreachable_proxy_hostname(tmp_path: Path) -> None:
    deps = _deps(tmp_path, profile={**_HOST_PROFILE, "container_network": "host"})
    result = _by_name(_run(tmp_path, deps=deps), "proxy_host_reachable")
    assert not result.ok
    assert "10.0.2.2" in result.detail


def test_host_network_accepts_rootlesskit_proxy_address(tmp_path: Path) -> None:
    job = replace(_job(), container_host="10.0.2.2")
    deps = _deps(tmp_path, profile={**_HOST_PROFILE, "container_network": "host"})
    result = _by_name(_run(tmp_path, job=job, deps=deps), "proxy_host_reachable")
    assert result.ok


def test_host_network_with_concurrent_trials_fails_launch_check(tmp_path: Path) -> None:
    deps = _deps(tmp_path, profile={**_HOST_PROFILE, "container_network": "host"})

    result = _by_name(
        _run(tmp_path, job=_job(concurrency=2), deps=deps), "host_network_concurrency"
    )

    assert not result.ok
    assert "concurrency: 1" in result.detail


def test_host_network_with_no_fixed_task_ports_allows_concurrent_trials(tmp_path: Path) -> None:
    deps = _deps(tmp_path, profile={**_HOST_PROFILE, "container_network": "host"})
    job = _job(concurrency=8)
    job = replace(job, tasks=replace(job.tasks, host_ports="none"))

    result = _by_name(_run(tmp_path, job=job, deps=deps), "host_network_concurrency")

    assert result.ok
    assert "tasks declare no host ports (tasks.host_ports: none); concurrency 8" in result.detail


def test_host_network_unknown_task_ports_explains_declaration(tmp_path: Path) -> None:
    deps = _deps(tmp_path, profile={**_HOST_PROFILE, "container_network": "host"})

    result = _by_name(
        _run(tmp_path, job=_job(concurrency=8), deps=deps), "host_network_concurrency"
    )

    assert not result.ok
    assert "tasks.host_ports: none" in result.detail


def test_host_network_with_one_trial_passes_launch_check(tmp_path: Path) -> None:
    deps = _deps(tmp_path, profile={**_HOST_PROFILE, "container_network": "host"})

    result = _by_name(
        _run(tmp_path, job=_job(concurrency=1), deps=deps), "host_network_concurrency"
    )

    assert result.ok


def test_bridge_network_allows_concurrent_trials(tmp_path: Path) -> None:
    deps = _deps(tmp_path, profile={**_HOST_PROFILE, "container_network": "bridge"})

    result = _by_name(
        _run(tmp_path, job=_job(concurrency=32), deps=deps), "host_network_concurrency"
    )

    assert result.ok


def test_host_network_concurrency_check_is_in_both_launch_check_lists(
    tmp_path: Path,
) -> None:
    deps = _deps(tmp_path)
    job = _job()

    run_names = [result.name for result in _run(tmp_path, job=job, deps=deps)]
    machine_names = [
        result.name for result in machine_check_launch_checks(job, tmp_path / "runs", deps)
    ]

    assert "host_network_concurrency" in run_names
    assert "host_network_concurrency" in machine_names


def test_memory_fits_fails_closed_for_task_without_memory_limit(tmp_path: Path) -> None:
    machine_check = _machine_check(
        tasks=[{"name": "task-a", "memory_mb": None}],
        included=["task-a"],
    )
    deps = _deps(tmp_path, read_machine_check=lambda _path: machine_check)

    result = _by_name(_run(tmp_path, deps=deps), "memory_fits")

    assert not result.ok
    assert "task-a" in result.detail
    assert "unbounded" in result.detail


def test_memory_fits_asks_for_a_new_machine_check_when_the_file_predates_memory(
    tmp_path: Path,
) -> None:
    machine_check = _machine_check(tasks=[{"name": "task-a"}], included=["task-a"])
    deps = _deps(tmp_path, read_machine_check=lambda _path: machine_check)

    result = _by_name(_run(tmp_path, deps=deps), "memory_fits")

    assert not result.ok
    assert result.detail == (
        "the machine check predates task memory recording; rerun `hrr check-machine`"
    )
    assert "unbounded" not in result.detail


def test_memory_fits_rejects_boolean_task_memory(tmp_path: Path) -> None:
    machine_check = _machine_check(
        tasks=[{"name": "task-a", "memory_mb": True}],
        included=["task-a"],
    )
    deps = _deps(tmp_path, read_machine_check=lambda _path: machine_check)

    result = _by_name(_run(tmp_path, deps=deps), "memory_fits")

    assert not result.ok
    assert "declares no memory limit" in result.detail


def test_memory_fits_fails_when_docker_memory_cannot_be_read(tmp_path: Path) -> None:
    def unavailable() -> int:
        raise RuntimeError("docker memory unavailable")

    result = _by_name(
        _run(tmp_path, deps=_deps(tmp_path, read_docker_memory=unavailable)), "memory_fits"
    )

    assert not result.ok
    assert "docker memory unavailable" in result.detail


def test_memory_fits_fails_when_machine_check_is_unavailable(tmp_path: Path) -> None:
    def missing(_path: Path) -> MachineCheck:
        raise FileNotFoundError("machine check missing")

    result = _by_name(
        _run(tmp_path, deps=_deps(tmp_path, read_machine_check=missing)), "memory_fits"
    )

    assert not result.ok
    assert "machine check is unavailable" in result.detail


def test_proxy_port_free_check_fails_when_bind_fails(tmp_path: Path) -> None:
    def fail_to_bind(_host: str, _port: int) -> None:
        raise OSError("Address already in use")

    results = _run(tmp_path, deps=_deps(tmp_path, bind_port=fail_to_bind))
    result = _by_name(results, "proxy_port_free")

    assert not result.ok
    assert "8766" in result.detail


def test_proxy_port_free_check_passes_when_bind_succeeds(tmp_path: Path) -> None:
    results = _run(tmp_path, deps=_deps(tmp_path, bind_port=lambda _host, _port: None))
    result = _by_name(results, "proxy_port_free")

    assert result.ok


def test_proxy_port_free_check_uses_the_configured_host_and_port(tmp_path: Path) -> None:
    job = _job()

    def bind(host: str, port: int) -> None:
        if (host, port) != (job.proxy_host, job.proxy_port):
            raise OSError("wrong address")

    result = proxy_port_free_check(job, _deps(tmp_path, bind_port=bind))

    assert result[0]


def test_proxy_port_free_is_a_run_check_and_not_a_machine_check(tmp_path: Path) -> None:
    deps = _deps(tmp_path)
    job = _job()
    run_results = _run(tmp_path, deps=deps)
    machine_results = machine_check_launch_checks(job, tmp_path / "runs", deps)
    deps.host_lock.release()

    assert "proxy_port_free" in {result.name for result in run_results}
    assert "proxy_port_free" not in {result.name for result in machine_results}


def test_run_folder_check_fails_with_pids_that_name_the_run(tmp_path: Path) -> None:
    run_dir = tmp_path / "runs" / "study"
    results = _run(
        tmp_path,
        run_dir=run_dir,
        deps=_deps(tmp_path, processes=lambda path: [123, 456] if path == run_dir else []),
    )
    result = _by_name(results, "run_folder_not_live")

    assert not result.ok
    assert "123" in result.detail
    assert "456" in result.detail


def test_run_folder_check_passes_when_no_process_names_the_run(tmp_path: Path) -> None:
    results = _run(
        tmp_path,
        run_dir=tmp_path / "runs" / "study",
        deps=_deps(tmp_path, processes=lambda _path: []),
    )

    assert _by_name(results, "run_folder_not_live").ok


@pytest.mark.parametrize("failure", ["unlisted", "wrong-architecture"])
def test_unlisted_or_mismatched_image_refuses_launch(tmp_path: Path, failure: str) -> None:
    arm = _job(harness="opencode")
    wrong_arch_entry = ImageEntry(
        harness="opencode",
        commit=_COMMIT,
        version="1.19.3",
        arch="arm64",
        ref="hrr/opencode:test-arm64",
        digest=_DIGEST,
        build_method="source",
        built_at="2026-11-11",
        withdrawn=False,
        withdrawn_reason="",
    )
    manifest = {} if failure == "unlisted" else {("opencode", _COMMIT, "arm64"): wrong_arch_entry}
    results = _run(tmp_path, job=arm, manifest=manifest)
    result = _by_name(results, "arms_pinned")

    assert not result.ok
    if failure == "unlisted":
        assert "not listed" in result.detail
    else:
        assert "architecture" in result.detail


def test_arms_pinned_check_passes_for_manifest_free_fake_harness(tmp_path: Path) -> None:
    result = _by_name(_run(tmp_path), "arms_pinned")

    assert result.ok


def test_arms_pinned_check_refuses_task_architecture_missing_from_manifest(
    tmp_path: Path,
) -> None:
    task = {"name": "task-a", "digest": "task-digest", "arch": "amd64"}
    machine_check = _machine_check(tasks=[task])
    deps = _deps(tmp_path, read_machine_check=lambda _path: machine_check)
    arm = _job(harness="opencode")
    arm64_entry = ImageEntry(
        harness="opencode",
        commit=_COMMIT,
        version="1.19.3",
        arch="arm64",
        ref="hrr/opencode:test-arm64",
        digest=_DIGEST,
        build_method="source",
        built_at="2026-11-11",
        withdrawn=False,
        withdrawn_reason="",
    )

    result = _by_name(
        _run(
            tmp_path,
            job=arm,
            manifest={("opencode", _COMMIT, "arm64"): arm64_entry},
            deps=deps,
            arch="arm64",
        ),
        "arms_pinned",
    )

    assert not result.ok
    assert "amd64" in result.detail


def test_arms_pinned_check_accepts_all_task_architectures(tmp_path: Path) -> None:
    machine_check = _machine_check(
        tasks=[{"name": "task-a", "digest": "task-digest", "arch": "amd64"}]
    )
    deps = _deps(tmp_path, read_machine_check=lambda _path: machine_check)
    second_commit = "c" * 40
    arm = _job(harness="opencode")
    arm = replace(arm, arms=(arm.arms[0], Arm("arm-b", "opencode", second_commit)))
    manifest = {
        ("opencode", commit, architecture): ImageEntry(
            harness="opencode",
            commit=commit,
            version="1.19.3",
            arch=architecture,
            ref=f"hrr/opencode:test-{commit[:1]}-{architecture}",
            digest=_DIGEST,
            build_method="source",
            built_at="2026-11-11",
            withdrawn=False,
            withdrawn_reason="",
        )
        for commit in (_COMMIT, second_commit)
        for architecture in ("amd64", "arm64")
    }

    result = _by_name(
        _run(tmp_path, job=arm, manifest=manifest, deps=deps, arch="arm64"),
        "arms_pinned",
    )

    assert result.ok


def test_arms_pinned_check_uses_host_architecture_for_old_machine_check(
    tmp_path: Path,
) -> None:
    machine_check = _machine_check(tasks=[{"name": "task-a", "digest": "task-digest"}])
    deps = _deps(tmp_path, read_machine_check=lambda _path: machine_check)
    arm = _job(harness="opencode")
    entry = ImageEntry(
        harness="opencode",
        commit=_COMMIT,
        version="1.19.3",
        arch="arm64",
        ref="hrr/opencode:test-arm64",
        digest=_DIGEST,
        build_method="source",
        built_at="2026-11-11",
        withdrawn=False,
        withdrawn_reason="",
    )

    result = _by_name(
        _run(
            tmp_path,
            job=arm,
            manifest={("opencode", _COMMIT, "arm64"): entry},
            deps=deps,
            arch="arm64",
        ),
        "arms_pinned",
    )

    assert result.ok


def test_arms_pinned_check_includes_canary_host_architecture(tmp_path: Path) -> None:
    machine_check = _machine_check(
        tasks=[{"name": "task-a", "digest": "task-digest", "arch": "amd64"}]
    )
    deps = _deps(tmp_path, read_machine_check=lambda _path: machine_check)
    arm = _job(harness="opencode")
    entry = ImageEntry(
        harness="opencode",
        commit=_COMMIT,
        version="1.19.3",
        arch="amd64",
        ref="hrr/opencode:test-amd64",
        digest=_DIGEST,
        build_method="source",
        built_at="2026-11-11",
        withdrawn=False,
        withdrawn_reason="",
    )

    result = _by_name(
        _run(
            tmp_path,
            job=arm,
            manifest={("opencode", _COMMIT, "amd64"): entry},
            deps=deps,
            arch="arm64",
        ),
        "arms_pinned",
    )

    assert not result.ok
    assert "arm64" in result.detail


def test_arms_pinned_check_accepts_responses_model_card_with_chat_harness(
    tmp_path: Path,
) -> None:
    result = _by_name(_run(tmp_path, card=_card(api="responses")), "arms_pinned")

    assert result.ok


def test_arms_pinned_check_refuses_unsupported_adapter_pair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from harbor_research_runner.run import checks

    monkeypatch.setattr(checks, "request_path", lambda _harness: "/v1/responses")
    result = _by_name(_run(tmp_path, card=_card(api="chat")), "arms_pinned")

    assert not result.ok
    assert "unsupported adapter pair" in result.detail


def test_docker_clean_check_fails_when_inventory_has_residue(tmp_path: Path) -> None:
    deps = _deps(
        tmp_path,
        inventory=lambda: Inventory(True, "ok", ("trial__env-main-1",), (), (), ()),
    )
    result = _by_name(_run(tmp_path, deps=deps), "docker_clean")

    assert not result.ok
    assert "trial__env-main-1" in result.detail


def test_docker_clean_check_fails_when_inventory_has_a_leaked_network(
    tmp_path: Path,
) -> None:
    deps = _deps(
        tmp_path,
        inventory=lambda: Inventory(True, "ok", (), ("trial__env_default",), (), ()),
    )
    result = _by_name(_run(tmp_path, deps=deps), "docker_clean")

    assert not result.ok
    assert "trial__env_default" in result.detail


def test_docker_clean_check_fails_when_daemon_is_unavailable(tmp_path: Path) -> None:
    deps = _deps(
        tmp_path,
        inventory=lambda: Inventory(False, "docker daemon unreachable", (), ()),
    )
    result = _by_name(_run(tmp_path, deps=deps), "docker_clean")

    assert not result.ok
    assert "docker daemon unreachable" in result.detail


def test_docker_clean_check_passes_for_clean_inventory(tmp_path: Path) -> None:
    result = _by_name(_run(tmp_path), "docker_clean")

    assert result.ok


def test_single_run_check_fails_when_another_session_holds_the_host_lock(
    tmp_path: Path,
) -> None:
    lock_path = tmp_path / "host.lock"
    holder = HostLock(lock_path)
    runner = HostLock(lock_path)
    holder.acquire()
    try:
        deps = _deps(
            tmp_path,
            host_lock=runner,
            live_run=lambda _runs_dir: "run-holder",
        )
        result = _by_name(_run(tmp_path, deps=deps), "single_run")
    finally:
        holder.release()

    assert not result.ok
    assert "run-holder" in result.detail


def test_single_run_check_uses_runs_directory_to_find_lock_holder(tmp_path: Path) -> None:
    lock_path = tmp_path / "host.lock"
    holder = HostLock(lock_path)
    holder.acquire()
    expected_runs_dir = tmp_path / "runs"
    try:
        deps = _deps(
            tmp_path,
            host_lock=HostLock(lock_path),
            live_run=lambda path: "run-holder" if path == expected_runs_dir else None,
        )
        result = _by_name(_run(tmp_path, deps=deps), "single_run")
    finally:
        holder.release()

    assert not result.ok
    assert "run-holder" in result.detail


def test_single_run_check_fails_closed_when_lock_holder_is_unknown(tmp_path: Path) -> None:
    lock_path = tmp_path / "host.lock"
    holder = HostLock(lock_path)
    holder.acquire()
    try:
        deps = _deps(
            tmp_path,
            host_lock=HostLock(lock_path),
            live_run=lambda _runs_dir: "unknown",
        )
        result = _by_name(_run(tmp_path, deps=deps), "single_run")
    finally:
        holder.release()

    assert not result.ok
    assert "unknown" in result.detail


def test_single_run_check_fails_when_lock_holder_lookup_errors(tmp_path: Path) -> None:
    lock_path = tmp_path / "host.lock"
    holder = HostLock(lock_path)
    holder.acquire()

    def unavailable(_runs_dir: Path) -> str:
        raise RuntimeError("process lookup unavailable")

    try:
        deps = _deps(
            tmp_path,
            host_lock=HostLock(lock_path),
            live_run=unavailable,
        )
        result = _by_name(_run(tmp_path, deps=deps), "single_run")
    finally:
        holder.release()

    assert result.ok is False
    assert "host lock is held" in result.detail


def test_single_run_check_reports_unsupported_lock_distinctly(tmp_path: Path) -> None:
    def unsupported_flock(_fd: int, _operation: int) -> None:
        raise OSError(errno.ENOLCK, "locking unsupported")

    lock = HostLock(tmp_path / "unsupported.lock", flock=unsupported_flock)
    result = _by_name(_run(tmp_path, deps=_deps(tmp_path, host_lock=lock)), "single_run")

    assert not result.ok
    assert "unsupported" in result.detail


def test_single_run_check_passes_and_keeps_lock_held(tmp_path: Path) -> None:
    deps = _deps(tmp_path)
    result = _by_name(_run(tmp_path, deps=deps, keep_lock=True), "single_run")

    assert result.ok
    assert deps.host_lock.held
    deps.host_lock.release()


def test_host_resources_check_fails_when_host_breaches_limits(tmp_path: Path) -> None:
    deps = _deps(tmp_path, measure=lambda **_kwargs: (_sample(swapped_out_pages=1), 0))
    result = _by_name(_run(tmp_path, deps=deps), "host_resources")

    assert not result.ok
    assert "breach" in result.detail


def test_host_resources_check_passes_when_host_is_within_limits(tmp_path: Path) -> None:
    result = _by_name(_run(tmp_path), "host_resources")

    assert result.ok


def test_host_resources_check_measures_runs_directory(tmp_path: Path) -> None:
    paths = []
    deps = _deps(tmp_path, measure=lambda *, disk_path: (paths.append(disk_path) or _sample(), 0))

    _run(tmp_path, deps=deps)

    assert paths == [tmp_path / "runs"]


def test_host_resources_check_passes_before_the_runs_directory_exists(tmp_path: Path) -> None:
    def measure(*, disk_path: Path) -> tuple[Sample, int]:
        shutil.disk_usage(disk_path)
        return _sample(), 0

    assert not (tmp_path / "runs").exists()
    result = _by_name(_run(tmp_path, deps=_deps(tmp_path, measure=measure)), "host_resources")

    assert result.ok, result.detail


def test_host_resources_check_creates_missing_parent_directories(tmp_path: Path) -> None:
    disk_path = tmp_path / "runs" / "nested"
    deps = _deps(tmp_path)

    result = host_resources_check(_job(), deps, 1.0, disk_path)

    assert result[0]
    assert disk_path.is_dir()


def test_host_resources_check_ignores_swapouts_before_launch(tmp_path: Path) -> None:
    (tmp_path / "runs").mkdir()
    vm_stat = (Path(__file__).parents[2] / "fixtures" / "vm_stat-macos.txt").read_text(
        encoding="utf-8"
    )

    def run(argv, **kwargs):
        if argv == ["vm_stat"]:
            return SimpleNamespace(returncode=0, stdout=vm_stat, stderr="")
        if argv[:2] == ["docker", "info"]:
            return SimpleNamespace(returncode=0, stdout="ok", stderr="")
        if argv[:2] == ["docker", "ps"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        raise AssertionError(argv)

    def read_text(path: Path, **kwargs):
        raise FileNotFoundError(path)

    deps = _deps(
        tmp_path,
        measure=lambda **kwargs: measure_host(run=run, read_text=read_text, **kwargs),
    )
    result = _by_name(_run(tmp_path, deps=deps), "host_resources")

    assert result.ok


def test_machine_check_present_fails_when_file_is_missing(tmp_path: Path) -> None:
    def missing(_path: Path) -> MachineCheck:
        raise FileNotFoundError("missing machine check")

    result = _by_name(
        _run(tmp_path, deps=_deps(tmp_path, read_machine_check=missing)),
        "machine_check_present",
    )

    assert not result.ok
    assert "machine check" in result.detail


def test_machine_check_matches_fails_when_file_is_missing(tmp_path: Path) -> None:
    def missing(_path: Path) -> MachineCheck:
        raise FileNotFoundError("missing machine check")

    result = _by_name(
        _run(tmp_path, deps=_deps(tmp_path, read_machine_check=missing)),
        "machine_check_matches",
    )

    assert not result.ok
    assert "machine check is unavailable" in result.detail


def test_machine_check_present_fails_when_file_is_unparseable(tmp_path: Path) -> None:
    def invalid(_path: Path) -> MachineCheck:
        raise ValueError("invalid machine check JSON")

    result = _by_name(
        _run(tmp_path, deps=_deps(tmp_path, read_machine_check=invalid)),
        "machine_check_present",
    )

    assert not result.ok
    assert "invalid machine check" in result.detail


def test_machine_check_present_passes_when_file_parses(tmp_path: Path) -> None:
    result = _by_name(_run(tmp_path), "machine_check_present")

    assert result.ok


def test_run_checks_reads_machine_check_from_configured_path(tmp_path: Path) -> None:
    job = _job()

    def read_machine_check(path: Path) -> MachineCheck:
        if path != job.machine_check:
            raise FileNotFoundError(path)
        return _machine_check()

    results = _run(tmp_path, job=job, deps=_deps(tmp_path, read_machine_check=read_machine_check))

    assert _by_name(results, "machine_check_present").ok
    assert _by_name(results, "machine_check_matches").ok


@pytest.mark.parametrize(
    "machine_check",
    [
        _machine_check(task_source={"dataset": "other/tasks"}),
        _machine_check(harbor_version="0.22.0"),
        _machine_check(host_profile={**_HOST_PROFILE, "memory_cap": "none"}),
        _machine_check(concurrency=1),
    ],
    ids=["task-source", "harbor-version", "host-profile", "concurrency"],
)
def test_machine_check_matches_fails_when_check_differs(
    tmp_path: Path, machine_check: MachineCheck
) -> None:
    deps = _deps(tmp_path, read_machine_check=lambda _path: machine_check)
    result = _by_name(_run(tmp_path, deps=deps), "machine_check_matches")

    assert not result.ok


def test_machine_check_matches_passes_for_matching_check(tmp_path: Path) -> None:
    result = _by_name(_run(tmp_path), "machine_check_matches")

    assert result.ok


def test_machine_check_matches_refuses_when_path_task_content_changes(tmp_path: Path) -> None:
    task_root = tmp_path / "tasks"
    task_dir = task_root / "task-a"
    task_dir.mkdir(parents=True)
    instruction = task_dir / "instruction.md"
    instruction.write_text("original instruction", encoding="utf-8")
    digest, _ = Packager.compute_content_hash(task_dir)
    check = _machine_check(
        task_source={"path": str(task_root)},
        included=["task-a"],
        tasks=[{"name": "task-a", "memory_mb": 512, "digest": f"sha256:{digest}"}],
    )
    job = replace(
        _job(include=("task-a",)),
        tasks=TaskSource("path", None, str(task_root), None, None, ("task-a",), ()),
    )
    instruction.write_text("edited instruction", encoding="utf-8")

    result = _by_name(
        _run(
            tmp_path,
            job=job,
            deps=_deps(tmp_path, read_machine_check=lambda _path: check),
        ),
        "machine_check_matches",
    )

    assert not result.ok
    assert result.detail == (
        "path task content differs from the machine check for task-a; rerun `hrr check-machine`"
    )


def test_machine_check_matches_lists_every_missing_path_task(tmp_path: Path) -> None:
    task_root = tmp_path / "tasks"
    task_root.mkdir()
    check = _machine_check(
        task_source={"path": str(task_root)},
        included=["task-a", "task-b"],
        tasks=[
            {"name": "task-a", "memory_mb": 512},
            {"name": "task-b", "memory_mb": 512},
        ],
    )
    job = replace(
        _job(include=("task-a", "task-b")),
        tasks=TaskSource("path", None, str(task_root), None, None, ("task-a", "task-b"), ()),
    )

    result = _by_name(
        _run(tmp_path, job=job, deps=_deps(tmp_path, read_machine_check=lambda _path: check)),
        "machine_check_matches",
    )

    assert not result.ok
    assert result.detail == (
        "path task content differs from the machine check for task-a, task-b; "
        "rerun `hrr check-machine`"
    )


def test_machine_check_matches_reports_path_tasks_that_are_not_folders(
    tmp_path: Path,
) -> None:
    task_root = tmp_path / "tasks"
    task_root.mkdir()
    (task_root / "task-a").write_text("not a task folder", encoding="utf-8")
    check = _machine_check(
        task_source={"path": str(task_root)},
        included=["task-a", "task-b"],
        tasks=[
            {"name": "task-a", "memory_mb": 512, "digest": "sha256:0"},
            {"name": "task-b", "memory_mb": 512, "digest": "sha256:0"},
        ],
    )
    job = replace(
        _job(include=("task-a", "task-b")),
        tasks=TaskSource("path", None, str(task_root), None, None, ("task-a", "task-b"), ()),
    )

    result = _by_name(
        _run(tmp_path, job=job, deps=_deps(tmp_path, read_machine_check=lambda _path: check)),
        "machine_check_matches",
    )

    assert not result.ok
    assert result.detail == (
        "path task content differs from the machine check for task-a, task-b; "
        "rerun `hrr check-machine`"
    )


def test_withdrawn_model_card_without_reason_has_default_detail(tmp_path: Path) -> None:
    card = replace(_card(withdrawn=True), reason=None)

    result = _by_name(_run(tmp_path, card=card), "model_card")

    assert not result.ok
    assert result.detail == "model card is withdrawn"


def test_machine_check_matches_passes_for_unchanged_path_task(tmp_path: Path) -> None:
    task_root = tmp_path / "tasks"
    task_dir = task_root / "task-a"
    task_dir.mkdir(parents=True)
    (task_dir / "instruction.md").write_text("original instruction", encoding="utf-8")
    digest, _ = Packager.compute_content_hash(task_dir)
    check = _machine_check(
        task_source={"path": str(task_root)},
        included=["task-a"],
        tasks=[{"name": "task-a", "memory_mb": 512, "digest": f"sha256:{digest}"}],
    )
    job = replace(
        _job(include=("task-a",)),
        tasks=TaskSource("path", None, str(task_root), None, None, ("task-a",), ()),
    )

    result = _by_name(
        _run(
            tmp_path,
            job=job,
            deps=_deps(tmp_path, read_machine_check=lambda _path: check),
        ),
        "machine_check_matches",
    )

    assert result.ok


def test_machine_check_matches_refuses_check_without_host_profile(tmp_path: Path) -> None:
    check = _machine_check(host_profile=None)
    result = _by_name(
        _run(tmp_path, deps=_deps(tmp_path, read_machine_check=lambda _path: check)),
        "machine_check_matches",
    )

    assert not result.ok
    assert result.detail == (
        "the machine check predates host profile recording; rerun `hrr check-machine`"
    )


def test_machine_check_matches_names_each_different_profile_field(tmp_path: Path) -> None:
    check = _machine_check(host_profile={**_HOST_PROFILE, "memory_cap": "kernel"})
    deps = _deps(
        tmp_path,
        read_machine_check=lambda _path: check,
        profile={**_HOST_PROFILE, "memory_cap": "none"},
    )

    result = _by_name(_run(tmp_path, deps=deps), "machine_check_matches")

    assert not result.ok
    assert "memory_cap differs: kernel -> none" in result.detail


def test_job_checks_refuse_task_outside_machine_check(tmp_path: Path) -> None:
    check = _machine_check(included=["task-a"])
    deps = _deps(tmp_path, read_machine_check=lambda _path: check)
    result = _by_name(_run(tmp_path, job=_job(include=("task-b",)), deps=deps), "tasks_included")

    assert not result.ok
    assert "task-b" in result.detail


def test_tasks_included_check_passes_when_requested_tasks_are_included(
    tmp_path: Path,
) -> None:
    result = _by_name(_run(tmp_path, job=_job(include=("task-a",))), "tasks_included")

    assert result.ok


def test_empty_task_include_means_all_included_tasks_pass(tmp_path: Path) -> None:
    result = _by_name(_run(tmp_path, job=_job(include=())), "tasks_included")

    assert result.ok


@pytest.mark.parametrize(
    ("included", "include", "exclude", "reason"),
    [
        ([], (), (), "machine check included no tasks"),
        (["task-a", "task-b"], (), ("task-a", "task-b"), "every included task"),
        (["task-a", "task-b"], ("task-c",), (), "not included in the machine check: task-c"),
    ],
)
def test_tasks_included_check_refuses_an_empty_resolved_selection(
    tmp_path: Path,
    included: list[str],
    include: tuple[str, ...],
    exclude: tuple[str, ...],
    reason: str,
) -> None:
    check = _machine_check(included=included)
    deps = _deps(tmp_path, read_machine_check=lambda _path: check)

    result = _by_name(
        _run(tmp_path, job=_job(include=include, exclude=exclude), deps=deps),
        "tasks_included",
    )

    assert not result.ok
    assert reason in result.detail


def test_run_checks_evaluates_every_check_after_an_early_failure(tmp_path: Path) -> None:
    deps = _deps(
        tmp_path,
        inventory=lambda: Inventory(True, "ok", ("trial__env-main-1",), (), (), ()),
        measure=lambda **_kwargs: (_sample(swapped_out_pages=1), 0),
    )
    results = _run(tmp_path, card=_card(withdrawn=True), deps=deps)

    assert [result.name for result in results] == [
        "children_resolve",
        "model_card",
        "keys_present",
        "arms_pinned",
        "docker_clean",
        "proxy_port_free",
        "run_folder_not_live",
        "single_run",
        "host_resources",
        "machine_check_present",
        "machine_check_matches",
        "tasks_included",
        "memory_fits",
        "host_headroom",
        "proxy_host_reachable",
        "host_network_concurrency",
    ]
    assert not _by_name(results, "model_card").ok
    assert not _by_name(results, "docker_clean").ok
    assert not _by_name(results, "host_resources").ok


def test_all_launch_checks_pass_for_a_ready_fake_harness(tmp_path: Path) -> None:
    results = _run(tmp_path)

    assert all(result.ok for result in results)


def test_resolve_tasks_preserves_machine_check_order_and_filters_include_exclude() -> None:
    job = _job(include=("task-c", "task-a"), exclude=("task-a",))
    check = _machine_check(included=["task-a", "task-b", "task-c"])

    assert resolve_tasks(job, check) == ["task-c"]


def test_resolve_tasks_empty_include_selects_all_machine_tasks_except_excluded() -> None:
    job = _job(include=(), exclude=("task-b",))
    check = _machine_check(included=["task-b", "task-a", "task-c"])

    assert resolve_tasks(job, check) == ["task-a", "task-c"]


def test_machine_check_launch_checks_pass_and_leave_host_lock_held(tmp_path: Path) -> None:
    deps = _deps(tmp_path)

    results = machine_check_launch_checks(_job(), tmp_path / "runs", deps)

    assert results == [
        CheckResult("children_resolve", True, "Python child and Harbor executable resolve"),
        CheckResult("docker_clean", True, "Docker inventory is clean"),
        CheckResult("single_run", True, "this process holds the host lock"),
        CheckResult("host_resources", True, "host resources meet the job limits"),
        CheckResult(
            "host_network_concurrency",
            True,
            "task container networking supports the job concurrency",
        ),
    ]
    assert deps.host_lock.held
    deps.host_lock.release()


def test_machine_check_launch_checks_reports_each_failure(tmp_path: Path) -> None:
    deps = _deps(
        tmp_path,
        inventory=lambda: Inventory(True, "ok", ("residue",), (), (), ()),
        measure=lambda **_kwargs: (_sample(swapped_out_pages=1), 0),
    )

    results = machine_check_launch_checks(_job(), tmp_path / "runs", deps)

    assert [result.name for result in results] == [
        "children_resolve",
        "docker_clean",
        "single_run",
        "host_resources",
        "host_network_concurrency",
    ]
    assert results[0].ok
    assert not results[1].ok
    assert results[2].ok
    assert not results[3].ok
    assert results[4].ok
    deps.host_lock.release()


def test_machine_check_single_run_reports_the_lock_holder(tmp_path: Path) -> None:
    lock_path = tmp_path / "host.lock"
    holder = HostLock(lock_path)
    holder.acquire()
    runs_dir = tmp_path / "runs"
    try:
        deps = _deps(
            tmp_path,
            host_lock=HostLock(lock_path),
            live_run=lambda path: "run-holder" if path == runs_dir else None,
        )
        results = machine_check_launch_checks(_job(), runs_dir, deps)
    finally:
        holder.release()

    assert not results[2].ok
    assert results[2].detail == "host lock is held by run run-holder"


@pytest.mark.parametrize("message", ["docker unavailable", ""])
def test_failed_check_records_exception_message_or_type(tmp_path: Path, message: str) -> None:
    def unavailable() -> Inventory:
        raise RuntimeError(message)

    result = _by_name(_run(tmp_path, deps=_deps(tmp_path, inventory=unavailable)), "docker_clean")

    assert result.ok is False
    assert result.detail == (message or "RuntimeError")
