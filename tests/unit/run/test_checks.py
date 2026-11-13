from __future__ import annotations

import errno
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

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
    machine_check_launch_checks,
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
    measure=lambda: (_sample(), 0),
    inventory=lambda: Inventory(True, "ok", (), (), (), ()),
    live_run=lambda _runs_dir: None,
    host_lock: HostLock | None = None,
    read_machine_check=lambda _path: _machine_check(),
    fingerprint: str = "fingerprint",
    profile: dict = _HOST_PROFILE,
    harbor_version: str = "0.23.0",
    read_docker_memory=lambda: 8123,
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
        bind_port=bind_port,
        processes=processes,
        digest_of=digest_of,
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
        tasks=[{"name": "task-a", "memory_mb": 2048}],
        included=["task-a"],
    )
    deps = _deps(
        tmp_path,
        read_machine_check=lambda _path: machine_check,
    )

    result = _by_name(_run(tmp_path, job=_job(concurrency=4), deps=deps), "memory_fits")

    assert not result.ok
    assert "4" in result.detail
    assert "2048" in result.detail
    assert "4096" in result.detail
    assert "8123" in result.detail


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
    assert "check-machine" in result.detail
    assert "unbounded" not in result.detail


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
    deps = _deps(tmp_path, measure=lambda: (_sample(mem_available_mb=1024), 0))
    result = _by_name(_run(tmp_path, deps=deps), "host_resources")

    assert not result.ok
    assert "breach" in result.detail


def test_host_resources_check_passes_when_host_is_within_limits(tmp_path: Path) -> None:
    result = _by_name(_run(tmp_path), "host_resources")

    assert result.ok


def test_host_resources_check_ignores_swapouts_before_launch(tmp_path: Path) -> None:
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
        measure=lambda: measure_host(run=run, read_text=read_text, disk_path=tmp_path),
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


def test_machine_check_matches_refuses_check_without_host_profile(tmp_path: Path) -> None:
    check = _machine_check(host_profile=None)
    result = _by_name(
        _run(tmp_path, deps=_deps(tmp_path, read_machine_check=lambda _path: check)),
        "machine_check_matches",
    )

    assert not result.ok
    assert "rerun `hrr check-machine`" in result.detail


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


def test_run_checks_evaluates_every_check_after_an_early_failure(tmp_path: Path) -> None:
    deps = _deps(
        tmp_path,
        inventory=lambda: Inventory(True, "ok", ("trial__env-main-1",), (), (), ()),
        measure=lambda: _sample(mem_available_mb=1024),
    )
    results = _run(tmp_path, card=_card(withdrawn=True), deps=deps)

    assert [result.name for result in results] == [
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
        "proxy_host_reachable",
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
        CheckResult("docker_clean", True, "Docker inventory is clean"),
        CheckResult("single_run", True, "this process holds the host lock"),
        CheckResult("host_resources", True, "host resources meet the job limits"),
    ]
    assert deps.host_lock.held
    deps.host_lock.release()


def test_machine_check_launch_checks_reports_each_failure(tmp_path: Path) -> None:
    deps = _deps(
        tmp_path,
        inventory=lambda: Inventory(True, "ok", ("residue",), (), (), ()),
        measure=lambda: _sample(mem_available_mb=1024),
    )

    results = machine_check_launch_checks(_job(), tmp_path / "runs", deps)

    assert [result.name for result in results] == [
        "docker_clean",
        "single_run",
        "host_resources",
    ]
    assert not results[0].ok
    assert results[1].ok
    assert not results[2].ok
    deps.host_lock.release()
