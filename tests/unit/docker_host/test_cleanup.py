import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from harbor_research_runner.docker_host.cleanup import (
    RunActive,
    clean,
    clean_trials,
    compose_project_name,
    inventory,
    is_leaked_network,
    is_pool_exhaustion_text,
    is_residue_container,
    live_run,
)
from harbor_research_runner.docker_host.host_lock import HostLock

CONTAINERS = (
    "caffe-cifar-10__wjtwrfe__env-main-1",
    "qemu-alpine-ssh__kmgi4ef__env-main-1",
    "qemu-alpine-ssh__un7dou9__env-main-1",
    "csv-to-json__xhdqbwg__env-main-1",
    "csv-to-json__jfe25tm__env-main-1",
)
NETWORKS = (
    "train-fasttext__a1b2c3d__env_default",
    "rstan-to-pystan__e4f5g6h__env_default",
    "regex-chess__i7j8k9l__env_default",
    "qemu-alpine-ssh__kmgi4ef__env_default",
    "qemu-alpine-ssh__un7dou9__env_default",
    "csv-to-json__xhdqbwg__env_default",
)


def docker_run(containers=CONTAINERS, networks=NETWORKS, attached=None, calls=None):
    attached = attached or {}
    calls = calls if calls is not None else []

    def run(argv, **kwargs):
        calls.append(list(argv))
        if argv[:2] == ["docker", "info"]:
            return SimpleNamespace(returncode=0, stdout="29.7.2\n", stderr="")
        if argv[:2] == ["docker", "ps"]:
            return SimpleNamespace(returncode=0, stdout="\n".join(containers), stderr="")
        if argv[:3] == ["docker", "network", "ls"]:
            return SimpleNamespace(returncode=0, stdout="\n".join(networks), stderr="")
        if argv[:3] == ["docker", "network", "inspect"]:
            data = {str(i): {"Name": name} for i, name in enumerate(attached.get(argv[3], []))}
            return SimpleNamespace(returncode=0, stdout=json.dumps(data), stderr="")
        if argv[1:2] in (["stop"], ["rm"]) or argv[:3] == ["docker", "network", "rm"]:
            return SimpleNamespace(returncode=0, stdout=argv[-1], stderr="")
        raise AssertionError(argv)

    return run, calls


def test_hostops_classifier_matches_live_shapes():
    assert all(is_residue_container(name) for name in CONTAINERS)
    assert all(is_leaked_network(name) for name in NETWORKS)
    assert not is_residue_container("myapp__env-main")
    assert not is_leaked_network("myapp_default")


def test_inventory_lists_exact_live_residue():
    run, _ = docker_run()
    result = inventory(run)
    assert result.daemon_ok
    assert result.residue_containers == CONTAINERS
    assert result.leaked_networks == NETWORKS


def test_inventory_reports_daemon_failure():
    def run(argv, **kwargs):
        return SimpleNamespace(returncode=1, stdout="", stderr="Cannot connect to daemon")

    result = inventory(run)
    assert not result.daemon_ok
    assert "daemon" in result.daemon_detail.lower()


def test_inventory_reports_missing_docker():
    def run(argv, **kwargs):
        raise FileNotFoundError("docker missing")

    assert not inventory(run).daemon_ok


def test_inventory_pool_exhaustion_surfaces_cleanup_command():
    def run(argv, **kwargs):
        if argv[:2] == ["docker", "info"]:
            return SimpleNamespace(returncode=0, stdout="29.7.2", stderr="")
        if argv[:2] == ["docker", "ps"]:
            return SimpleNamespace(
                returncode=1,
                stdout="",
                stderr="all predefined address pools have been fully subnetted",
            )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    result = inventory(run)
    assert not result.daemon_ok
    assert "clean-docker --apply" in result.daemon_detail


def test_inventory_foreign_docker_failure_has_no_cleanup_hint():
    def run(argv, **kwargs):
        if argv[:2] == ["docker", "info"]:
            return SimpleNamespace(returncode=0, stdout="29.7.2", stderr="")
        if argv[:2] == ["docker", "ps"]:
            return SimpleNamespace(returncode=1, stdout="", stderr="connection reset")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    assert "clean-docker" not in inventory(run).daemon_detail


def test_inventory_wraps_docker_execution_errors():
    def run(argv, **kwargs):
        raise subprocess.SubprocessError("execution failed")

    result = inventory(run)
    assert not result.daemon_ok
    assert "unreachable" in result.daemon_detail


def test_exited_residue_endpoint_is_removable():
    network = "csv-to-json__xhdqbwg__env_default"
    run, _ = docker_run(
        containers=["csv-to-json__xhdqbwg__env-main-1"],
        networks=[network],
        attached={network: ["csv-to-json__xhdqbwg__env-main-1"]},
    )
    from harbor_research_runner.docker_host.cleanup import plan

    result = plan(inventory(run), None)
    assert result.planned_networks == (network,)


def test_clean_dry_run_mutates_nothing():
    run, calls = docker_run()
    result = clean(apply=False, lock=HostLock(Path("unused")), run=run)
    assert result.dry_run
    assert result.planned_containers == CONTAINERS
    assert not result.removed_containers
    assert not [
        c for c in calls if c[1:2] in (["stop"], ["rm"]) or c[:3] == ["docker", "network", "rm"]
    ]


def test_clean_apply_skips_attached_and_never_prunes(tmp_path):
    attached = {NETWORKS[3]: ["live-container"]}
    run, calls = docker_run(attached=attached)
    result = clean(apply=True, lock=HostLock(tmp_path / "lock"), run=run)
    assert not result.dry_run
    assert result.removed_containers == CONTAINERS
    assert NETWORKS[3] in result.skipped_attached
    assert NETWORKS[3] not in result.removed_networks
    assert not any("prune" in call or "image" in call or "volume" in call for call in calls)


def test_clean_dry_run_matches_apply_plan(tmp_path):
    attached = {NETWORKS[3]: ["live-container"]}
    dry_run, _ = docker_run(attached=attached)
    applied_run, _ = docker_run(attached=attached)
    dry = clean(apply=False, lock=HostLock(tmp_path / "unused"), run=dry_run)
    applied = clean(apply=True, lock=HostLock(tmp_path / "host.lock"), run=applied_run)
    assert dry.planned_containers == applied.planned_containers
    assert dry.planned_networks == applied.planned_networks
    assert dry.skipped_attached == applied.skipped_attached


def test_clean_trials_scopes_to_trial_names():
    selected = "make-mips-interpreter__a1b2c3d"
    selected_container = f"{selected}__env-main-1"
    selected_network = f"{selected}__env_default"
    foreign = "query-optimize__e4f5g6h__env-main-1"
    run, _ = docker_run(
        [selected_container, foreign],
        [selected_network, "query-optimize__e4f5g6h__env_default"],
        {selected_network: ["live-container"]},
    )
    result = clean_trials(frozenset({selected}), run=run)
    assert result.planned_containers == (selected_container,)
    assert result.skipped_attached == (selected_network,)
    assert result.removed_containers == (selected_container,)


def test_clean_trials_matches_mixed_case_trial_names():
    trial = "csv-to-json__rJP6mcs"
    container = "csv-to-json__rjp6mcs__env-main-1"
    network = "csv-to-json__rjp6mcs__env_default"
    run, _ = docker_run([container], [network])
    result = clean_trials(frozenset({trial}), run=run)
    assert result.removed_containers == (container,)
    assert result.removed_networks == (network,)


def test_clean_trials_matches_dotted_task_names():
    trial = "install-windows-3.11__wfsu3Ua"
    container = "install-windows-3-11__wfsu3ua__env-main-1"
    network = "install-windows-3-11__wfsu3ua__env_default"
    run, _ = docker_run([container], [network])
    result = clean_trials(frozenset({trial}), run=run)
    assert result.removed_containers == (container,)
    assert result.removed_networks == (network,)


@pytest.mark.parametrize(
    "trial", ["install-windows-3.11__wfsu3Ua", "csv-to-json__rJP6mcs", ".hidden__AbC", "a b__x+y"]
)
def test_compose_project_name_matches_harbor(trial):
    from harbor.environments.docker.docker import _sanitize_docker_compose_project_name

    assert compose_project_name(trial) == _sanitize_docker_compose_project_name(trial)


def test_unknown_network_inspection_is_skipped():
    network = NETWORKS[0]

    def run(argv, **kwargs):
        if argv[:2] == ["docker", "info"]:
            return SimpleNamespace(returncode=0, stdout="29.7.2", stderr="")
        if argv[:2] == ["docker", "ps"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if argv[:3] == ["docker", "network", "ls"]:
            return SimpleNamespace(returncode=0, stdout=network, stderr="")
        return SimpleNamespace(returncode=1, stdout="", stderr="no such network")

    from harbor_research_runner.docker_host.cleanup import plan

    result = plan(inventory(run), None)
    assert not result.planned_networks
    assert result.skipped_attached == (network,)


def test_pool_exhaustion_text_is_detected():
    assert is_pool_exhaustion_text("all predefined address pools have been fully subnetted")
    assert not is_pool_exhaustion_text("connection reset")


def _write_events(run_dir, events):
    run_dir.mkdir(parents=True)
    (run_dir / "run-events.jsonl").write_text(
        "".join(json.dumps(event) + "\n" for event in events), encoding="utf-8"
    )


def test_live_launch_with_live_pid_names_run(tmp_path):
    _write_events(tmp_path / "runs" / "study-1", [{"type": "launch", "pid": 321}])
    assert (
        live_run(
            tmp_path / "runs",
            pid_alive=lambda pid: pid == 321,
            processes=lambda _run_dir: [],
        )
        == "study-1"
    )


def test_live_launch_after_finish_does_not_name_run(tmp_path):
    _write_events(
        tmp_path / "runs" / "study-1", [{"type": "launch", "pid": 321}, {"type": "finish"}]
    )
    assert (
        live_run(tmp_path / "runs", pid_alive=lambda pid: True, processes=lambda _run_dir: [])
        is None
    )


def test_live_launch_with_dead_pid_does_not_name_run(tmp_path):
    _write_events(tmp_path / "runs" / "study-1", [{"type": "launch", "pid": 321}])
    assert (
        live_run(tmp_path / "runs", pid_alive=lambda pid: False, processes=lambda _run_dir: [])
        is None
    )


def test_live_launch_with_dead_resume_event_still_names_the_live_run(tmp_path):
    _write_events(
        tmp_path / "runs" / "study-1",
        [{"type": "launch", "pid": 111}, {"type": "resume", "pid": 222}],
    )
    assert (
        live_run(
            tmp_path / "runs",
            pid_alive=lambda pid: pid == 111,
            processes=lambda _run_dir: [],
        )
        == "study-1"
    )


def test_unreadable_events_name_an_unknown_holder(tmp_path):
    run_dir = tmp_path / "runs" / "study-1"
    _write_events(run_dir, [{"type": "launch", "pid": 321}])
    (run_dir / "run-events.jsonl").write_text("{broken", encoding="utf-8")
    assert live_run(tmp_path / "runs", processes=lambda _run_dir: []) == "unknown"


def test_pid_permission_error_names_an_unknown_holder(tmp_path):
    _write_events(tmp_path / "runs" / "study-1", [{"type": "launch", "pid": 321}])

    def inaccessible(pid):
        raise PermissionError(pid)

    assert (
        live_run(tmp_path / "runs", pid_alive=inaccessible, processes=lambda _run_dir: [])
        == "unknown"
    )


def test_live_command_names_run_even_when_runner_pid_is_dead(tmp_path):
    run_dir = tmp_path / "runs" / "study-1"
    _write_events(run_dir, [{"type": "launch", "pid": 321}, {"type": "finish"}])

    assert (
        live_run(
            tmp_path / "runs",
            pid_alive=lambda _pid: False,
            processes=lambda path: [654] if path == run_dir else [],
        )
        == "study-1"
    )


def test_clean_apply_refuses_while_a_session_holds_the_host_lock(tmp_path):
    lock_path = tmp_path / "host.lock"
    holder = HostLock(lock_path)
    holder.acquire()
    calls = []
    run, _ = docker_run(calls=calls)
    try:
        with pytest.raises(RunActive, match="host lock"):
            clean(apply=True, lock=HostLock(lock_path), run=run, runs_dir=tmp_path / "runs")
    finally:
        holder.release()
    assert not [
        c for c in calls if c[1:2] in (["stop"], ["rm"]) or c[:3] == ["docker", "network", "rm"]
    ]


def test_clean_apply_refusal_names_live_run_from_events(tmp_path):
    runs = tmp_path / "runs"
    _write_events(runs / "study-1", [{"type": "launch", "pid": os.getpid()}])
    holder = HostLock(tmp_path / "host.lock")
    holder.acquire()
    try:
        with pytest.raises(RunActive, match="study-1"):
            clean(
                apply=True,
                lock=HostLock(tmp_path / "host.lock"),
                run=docker_run()[0],
                runs_dir=runs,
                processes=lambda _run_dir: [],
            )
    finally:
        holder.release()


def test_clean_apply_leaves_resources_when_process_names_run_folder(tmp_path):
    runs = tmp_path / "runs"
    run_dir = runs / "study-1"
    _write_events(run_dir, [{"type": "launch", "pid": 321}, {"type": "finish"}])
    calls = []
    run, _ = docker_run(calls=calls)

    with pytest.raises(RunActive, match="study-1"):
        clean(
            apply=True,
            lock=HostLock(tmp_path / "host.lock"),
            run=run,
            runs_dir=runs,
            processes=lambda path: [654] if path == run_dir else [],
        )

    assert not any(call[1:2] in (["stop"], ["rm"]) for call in calls)


def test_clean_apply_plans_under_the_lock(tmp_path):
    lock_path = tmp_path / "host.lock"
    lock = HostLock(lock_path)
    calls = []
    dry_run, _ = docker_run([], [], calls=[])
    from harbor_research_runner.docker_host.cleanup import inventory as get_inventory
    from harbor_research_runner.docker_host.cleanup import plan

    snapshot = plan(get_inventory(dry_run), None)
    assert not snapshot.planned_containers
    run, _ = docker_run([CONTAINERS[0]], [], calls=calls)
    report = clean(apply=True, lock=lock, run=run)
    assert report.removed_containers == (CONTAINERS[0],)


def test_clean_apply_tolerates_residue_gone_since_snapshot(tmp_path):
    run, calls = docker_run([], [])
    report = clean(apply=True, lock=HostLock(tmp_path / "host.lock"), run=run)
    assert not report.removed_containers
    assert not [
        c for c in calls if c[1:2] in (["stop"], ["rm"]) or c[:3] == ["docker", "network", "rm"]
    ]
