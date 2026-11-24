from __future__ import annotations

import hashlib
import json
import logging
import os
import stat
import sys
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from harbor_research_runner.docker_host.cleanup import Inventory
from harbor_research_runner.docker_host.host_lock import HostLock
from harbor_research_runner.job_config.job_file import Arm, Job, TaskSource, load_job
from harbor_research_runner.job_config.model_cards import ModelCard
from harbor_research_runner.records import jsonl
from harbor_research_runner.records.limits import Limits
from harbor_research_runner.records.machine_check import MachineCheck
from harbor_research_runner.records.resource_stats import Sample
from harbor_research_runner.records.run_events import (
    Finish,
    Halt,
    HarborExit,
    HarborStart,
    LaunchCheck,
    SetAside,
    TrialVerdict,
)
from harbor_research_runner.run.checks import CheckDeps, CheckResult
from harbor_research_runner.run.loop import (
    RefusedToStart,
    RunDeps,
    RunOutcome,
    RunSession,
    _pass_loop,
    make_read_only,
    resume_run,
    run_job,
    sampler_command,
    slots_status,
)
from harbor_research_runner.run.tool_identity import ToolIdentity
from harbor_research_runner.run.watch import WatchResult


def test_sampler_command_writes_inside_the_run_directory(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"

    command = sampler_command(run_dir, 42)

    assert command[0] == sys.executable
    assert "uv" not in command
    assert command[command.index("--out") + 1] == str(run_dir / "resource-stats.jsonl")
    assert command[command.index("--interval") + 1] == "10.0"


_PROFILE = {
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


def _bind_port(_host: str, _port: int) -> None:
    return None


def verdict(
    trial: str,
    task: str,
    arm: str,
    *,
    valid: bool,
    measured: bool = True,
) -> dict:
    return TrialVerdict(
        t=1.0,
        trial=trial,
        task=task,
        arm=arm,
        valid=valid,
        reason=None if valid else "upstream_errors",
        instrument=None if valid else "proxy",
        labels=[],
        measured=measured,
        unmeasured_reason=None if measured else "ledger_damaged",
    ).to_row()


def test_slots_status_marks_fully_valid_slots_complete() -> None:
    status = slots_status(
        [("task-a", "arm-a")],
        2,
        [
            verdict("trial-a", "task-a", "arm-a", valid=True),
            verdict("trial-b", "task-a", "arm-a", valid=True),
        ],
        [],
        {"trial-a", "trial-b"},
        Limits(max_set_asides_per_slot=2, verifier_timeout_multiplier=1.0),
    )

    assert status.complete == [("task-a", "arm-a")]
    assert status.incomplete == []


def test_slots_status_ignores_non_verdict_rows_with_trial_names() -> None:
    status = slots_status(
        [("task-a", "arm-a")],
        1,
        [
            {"type": "launch", "trial": "trial-a", "task": "task-a", "arm": "arm-a"},
            verdict("trial-b", "task-a", "arm-a", valid=True),
        ],
        [],
        {"trial-a", "trial-b"},
        Limits(max_set_asides_per_slot=2, verifier_timeout_multiplier=1.0),
    )

    assert status.complete == [("task-a", "arm-a")]
    assert status.unmeasured_final == []


def test_slots_status_allows_first_set_aside_at_a_cap_of_one() -> None:
    status = slots_status(
        [("task-a", "arm-a")],
        1,
        [verdict("trial-a", "task-a", "arm-a", valid=False)],
        [],
        {"trial-a"},
        Limits(max_set_asides_per_slot=1, verifier_timeout_multiplier=1.0),
    )

    assert status.to_set_aside == ["trial-a"]
    assert status.incomplete == [("task-a", "arm-a")]


def test_slots_status_uses_each_remaining_set_aside_at_the_cap() -> None:
    status = slots_status(
        [("task-a", "arm-a")],
        1,
        [
            verdict("trial-a", "task-a", "arm-a", valid=False),
            verdict("trial-b", "task-a", "arm-a", valid=False),
        ],
        [],
        {"trial-a", "trial-b"},
        Limits(max_set_asides_per_slot=2, verifier_timeout_multiplier=1.0),
    )

    assert status.to_set_aside == ["trial-a", "trial-b"]


def test_slots_status_sets_aside_invalid_trial_below_cap() -> None:
    status = slots_status(
        [("task-a", "arm-a")],
        1,
        [verdict("trial-a", "task-a", "arm-a", valid=False)],
        [],
        {"trial-a"},
        Limits(max_set_asides_per_slot=2, verifier_timeout_multiplier=1.0),
    )

    assert status.to_set_aside == ["trial-a"]


def test_slots_status_does_not_count_deferred_trial_against_set_aside_cap() -> None:
    status = slots_status(
        [("task-a", "arm-a")],
        1,
        [
            TrialVerdict(
                t=1.0,
                trial="trial-a",
                task="task-a",
                arm="arm-a",
                valid=False,
                reason="deferred",
                instrument=None,
                labels=[],
                measured=True,
                unmeasured_reason=None,
            ).to_row()
        ],
        [SetAside(1.0, "trial-old", "task-a", "arm-a", "deferred").to_row()],
        {"trial-a"},
        Limits(max_set_asides_per_slot=1, verifier_timeout_multiplier=1.0),
    )

    assert status.to_set_aside == ["trial-a"]


def test_slots_status_does_not_charge_deferred_set_asides_to_later_invalid_trials() -> None:
    status = slots_status(
        [("task-a", "arm-a")],
        1,
        [verdict("trial-a", "task-a", "arm-a", valid=False)],
        [SetAside(1.0, "trial-old", "task-a", "arm-a", "deferred").to_row()],
        {"trial-a"},
        Limits(max_set_asides_per_slot=1, verifier_timeout_multiplier=1.0),
    )

    assert status.to_set_aside == ["trial-a"]


def test_slots_status_does_not_count_run_halted_trial_against_set_aside_cap() -> None:
    run_halted = TrialVerdict(
        t=1.0,
        trial="trial-a",
        task="task-a",
        arm="arm-a",
        valid=False,
        reason="run_halted",
        instrument=None,
        labels=[],
        measured=True,
        unmeasured_reason=None,
    ).to_row()
    status = slots_status(
        [("task-a", "arm-a")],
        1,
        [run_halted],
        [SetAside(1.0, "trial-old", "task-a", "arm-a", "run_halted").to_row()],
        {"trial-a"},
        Limits(max_set_asides_per_slot=1, verifier_timeout_multiplier=1.0),
    )

    assert status.to_set_aside == ["trial-a"]


def test_slots_status_sets_aside_pending_deferred_trial_at_cap() -> None:
    deferred = TrialVerdict(
        t=1.0,
        trial="trial-a",
        task="task-a",
        arm="arm-a",
        valid=False,
        reason="deferred",
        instrument=None,
        labels=[],
        measured=True,
        unmeasured_reason=None,
    ).to_row()
    status = slots_status(
        [("task-a", "arm-a")],
        1,
        [deferred, verdict("trial-b", "task-a", "arm-a", valid=False)],
        [SetAside(1.0, "trial-old", "task-a", "arm-a", "upstream_errors").to_row()],
        {"trial-a", "trial-b"},
        Limits(max_set_asides_per_slot=1, verifier_timeout_multiplier=1.0),
    )

    assert status.to_set_aside == ["trial-a"]
    assert status.incomplete == [("task-a", "arm-a")]


def test_slots_status_keeps_invalid_trial_at_cap() -> None:
    status = slots_status(
        [("task-a", "arm-a")],
        1,
        [verdict("trial-a", "task-a", "arm-a", valid=False)],
        [SetAside(1.0, "earlier", "task-a", "arm-a", "upstream_errors").to_row()],
        {"trial-a"},
        Limits(max_set_asides_per_slot=1, verifier_timeout_multiplier=1.0),
    )

    assert status.to_set_aside == []
    assert status.incomplete == [("task-a", "arm-a")]


def test_slots_status_applies_set_aside_caps_to_the_matching_slot() -> None:
    status = slots_status(
        [("task-b", "arm-b")],
        1,
        [verdict("trial-b", "task-b", "arm-b", valid=False)],
        [
            SetAside(1.0, "trial-old-a", "task-b", "arm-b", "upstream_errors").to_row(),
            SetAside(2.0, "trial-old-b", "task-b", "arm-b", "upstream_errors").to_row(),
        ],
        {"trial-b"},
        Limits(max_set_asides_per_slot=2, verifier_timeout_multiplier=1.0),
    )

    assert status.to_set_aside == []


def test_slots_status_uses_only_remaining_set_aside_budget_for_slot() -> None:
    status = slots_status(
        [("task-a", "arm-a")],
        1,
        [
            verdict("trial-a", "task-a", "arm-a", valid=False),
            verdict("trial-b", "task-a", "arm-a", valid=False),
        ],
        [SetAside(1.0, "earlier", "task-a", "arm-a", "upstream_errors").to_row()],
        {"trial-a", "trial-b"},
        Limits(max_set_asides_per_slot=2, verifier_timeout_multiplier=1.0),
    )

    assert status.to_set_aside == ["trial-a"]


def test_slots_status_lists_valid_unmeasured_trial() -> None:
    status = slots_status(
        [("task-a", "arm-a")],
        1,
        [verdict("trial-a", "task-a", "arm-a", valid=True, measured=False)],
        [],
        {"trial-a"},
        Limits(max_set_asides_per_slot=2, verifier_timeout_multiplier=1.0),
    )

    assert status.complete == [("task-a", "arm-a")]
    assert status.unmeasured_final == ["trial-a"]


def test_run_folder_finish_clears_owner_write_bit(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    nested = run_dir / "nested"
    nested.mkdir(parents=True)
    file_path = nested / "record.json"
    file_path.write_text("ok", encoding="utf-8")

    make_read_only(run_dir)

    assert stat.S_IMODE(file_path.stat().st_mode) & stat.S_IWUSR == 0


def test_run_folder_finish_clears_write_bits_from_nested_directories(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    deepest = run_dir / "harbor-job" / "trial-a" / "verifier"
    deepest.mkdir(parents=True)
    (deepest / "link").symlink_to(tmp_path / "outside")
    (deepest / "reward.txt").write_text("1", encoding="utf-8")
    directories = (run_dir, run_dir / "harbor-job", run_dir / "harbor-job" / "trial-a", deepest)
    expected_modes = {path: stat.S_IMODE(path.stat().st_mode) & ~0o222 for path in directories}

    make_read_only(run_dir)

    assert {path: stat.S_IMODE(path.stat().st_mode) for path in directories} == expected_modes
    assert stat.S_IMODE((deepest / "reward.txt").stat().st_mode) == 0o444


def _chmod_refused_for(monkeypatch: pytest.MonkeyPatch, refused: Path) -> None:
    real_chmod = Path.chmod

    def chmod(self: Path, mode: int, *, follow_symlinks: bool = True) -> None:
        if self == refused:
            raise PermissionError(1, "Operation not permitted", str(self))
        real_chmod(self, mode, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(Path, "chmod", chmod)


def test_run_folder_finish_skips_a_file_written_by_a_container_user(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    run_dir = tmp_path / "run"
    verifier = run_dir / "harbor-job" / "trial-a" / "verifier"
    verifier.mkdir(parents=True)
    foreign = verifier / "reward.txt"
    foreign.write_text("1", encoding="utf-8")
    own = run_dir / "run-events.jsonl"
    own.write_text("{}", encoding="utf-8")
    _chmod_refused_for(monkeypatch, foreign)
    monkeypatch.setattr("os.getuid", lambda: foreign.stat().st_uid + 1)

    make_read_only(run_dir)

    assert "harbor-job/trial-a/verifier/reward.txt" in caplog.text


def test_run_folder_finish_raises_when_an_owned_file_refuses_chmod(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    own = run_dir / "run-events.jsonl"
    own.write_text("{}", encoding="utf-8")
    _chmod_refused_for(monkeypatch, own)

    with pytest.raises(PermissionError):
        make_read_only(run_dir)


class Session:
    def __init__(self, run_dir: Path, *, set_aside_limit: int = 2) -> None:
        self.run_dir = run_dir
        self.job = SimpleNamespace(
            task_order="as_listed",
            proxy_host="127.0.0.1",
            proxy_port=8766,
            repetitions=1,
        )
        self.limits = Limits(
            max_set_asides_per_slot=set_aside_limit,
            verifier_timeout_multiplier=1.0,
        )
        run_dir.mkdir(parents=True)
        (run_dir / "run-events.jsonl").touch()
        (run_dir / "harbor-job").mkdir()

    @property
    def lifeline_read_fd(self) -> int:
        return 0

    def event(self, event) -> None:
        fd = jsonl.open_for_append(self.run_dir / "run-events.jsonl")
        try:
            jsonl.append_row(fd, event.to_row())
        finally:
            import os

            os.close(fd)


class Process:
    def poll(self) -> int | None:
        return None

    def wait(self) -> int:
        return 0


def dependencies(
    tmp_path: Path,
    watch_fn: Callable,
    commands: list[list[str]],
    stopped: list[bool],
) -> RunDeps:
    def start_services(session, _restart) -> None:
        session._lifeline_read_fd = 0

    return RunDeps(
        checks=CheckDeps(
            select_adapter=lambda *_args: None,
            measure=lambda **_kwargs: (Sample(1.0, 4096, 0, 100.0, 0.0, 0, True), 0),
            inventory=lambda: None,
            live_run=lambda _path: None,
            host_lock=HostLock(tmp_path / "host.lock"),
            read_machine_check=lambda _path: None,
            host_fingerprint=lambda: "fingerprint",
            harbor_version=lambda: "0.23.0",
            docker_memory_mb=lambda: 32768,
            host_memory_mb=lambda: 32768,
            bind_port=_bind_port,
            processes=lambda _run_dir: [],
            children_resolve=lambda: (True, "children resolve"),
        ),
        tool=ToolIdentity("0.1.0", "a" * 40),
        host=lambda: _HOST,
        start_harbor=lambda argv, _cwd, _lifeline_fd: commands.append(argv) or Process(),
        watch=watch_fn,
        start_services=start_services,
        stop_services=lambda _session: stopped.append(True),
        now=lambda: 1.0,
    )


def _resume_dependencies(
    tmp_path: Path,
    machine_check: MachineCheck,
    watch_fn: Callable,
    commands: list[list[str]],
    stopped: list[bool],
    *,
    hostname: str = "node-a",
    profile: dict = _PROFILE,
) -> RunDeps:
    def start_services(session, _restart) -> None:
        session._lifeline_read_fd = 0

    return RunDeps(
        checks=CheckDeps(
            select_adapter=lambda *_args: object(),
            measure=lambda **_kwargs: (Sample(1.0, 4096, 0, 100.0, 0.0, 0, True), 0),
            inventory=lambda: Inventory(True, "ok", (), ()),
            live_run=lambda _path: None,
            host_lock=HostLock(tmp_path / "resume-host.lock"),
            read_machine_check=lambda _path: machine_check,
            host_fingerprint=lambda: "fingerprint",
            host_profile=lambda: profile,
            harbor_version=lambda: "0.23.0",
            docker_memory_mb=lambda: 32768,
            host_memory_mb=lambda: 32768,
            bind_port=_bind_port,
            processes=lambda _run_dir: [],
            children_resolve=lambda: (True, "children resolve"),
        ),
        tool=ToolIdentity("0.1.0", "a" * 40),
        host=lambda: {
            "fingerprint": "fingerprint",
            "hostname": hostname,
            "arch": "arm64",
            "cpus": 8,
            "memory_mb": 32768,
            "docker_version": "27.0.0",
            "host_profile": profile,
        },
        start_harbor=lambda argv, _cwd, _lifeline_fd: commands.append(argv) or Process(),
        watch=watch_fn,
        start_services=start_services,
        stop_services=lambda _session: stopped.append(True),
        now=lambda: 2.0,
    )


def _trial_dir(run_dir: Path, trial: str) -> None:
    trial_dir = run_dir / "harbor-job" / trial
    trial_dir.mkdir()
    (trial_dir / "config.json").write_text("{}", encoding="utf-8")


def _trial_verdict(trial: str, *, valid: bool) -> TrialVerdict:
    return TrialVerdict(
        t=1.0,
        trial=trial,
        task="task-a",
        arm="arm-a",
        valid=valid,
        reason=None if valid else "upstream_errors",
        instrument=None if valid else "proxy",
        labels=[],
        measured=True,
        unmeasured_reason=None,
    )


def test_halted_pass_writes_halt_event_and_returns_two(tmp_path: Path) -> None:
    session = Session(tmp_path / "run")
    commands: list[list[str]] = []
    stopped: list[bool] = []

    def watch_fn(current, _process, _job_dir, _started):
        current.event(Halt(1.0, "quota_exhausted", "quota row"))
        return WatchResult("halted", None, "quota_exhausted", "quota row")

    deps = dependencies(tmp_path, watch_fn, commands, stopped)

    result = _pass_loop(session, [("task-a", "arm-a")], deps, 0)

    events = jsonl.read_rows(session.run_dir / "run-events.jsonl").rows
    assert result == 2
    assert any(row["type"] == "halt" for row in events)
    assert stopped == [True]


def test_invalid_trial_is_set_aside_before_resume_pass(tmp_path: Path) -> None:
    session = Session(tmp_path / "run")
    commands: list[list[str]] = []
    stopped: list[bool] = []
    calls = 0

    def watch_fn(current, _process, _job_dir, _started):
        nonlocal calls
        calls += 1
        if calls == 1:
            _trial_dir(current.run_dir, "trial-invalid")
            current.event(_trial_verdict("trial-invalid", valid=False))
        else:
            _trial_dir(current.run_dir, "trial-valid")
            current.event(_trial_verdict("trial-valid", valid=True))
        return WatchResult("exited", 0, None, None)

    deps = dependencies(tmp_path, watch_fn, commands, stopped)

    result = _pass_loop(session, [("task-a", "arm-a")], deps, 0)

    events = jsonl.read_rows(session.run_dir / "run-events.jsonl").rows
    starts = [row for row in events if row.get("type") == "harbor_start" and row["job"] == "main"]
    assert result == 0
    assert len(starts) == 2
    assert commands[0][1] == "run"
    assert commands[1][1:3] == ["jobs", "resume"]
    assert [row["trial"] for row in events if row.get("type") == "set_aside"] == ["trial-invalid"]
    assert stat.S_IMODE(session.run_dir.stat().st_mode) & stat.S_IWUSR == 0


def test_finish_returns_three_when_slot_stays_incomplete(tmp_path: Path) -> None:
    session = Session(tmp_path / "run", set_aside_limit=1)
    commands: list[list[str]] = []
    stopped: list[bool] = []
    session.event(SetAside(0.5, "earlier", "task-a", "arm-a", "upstream_errors"))

    def watch_fn(current, _process, _job_dir, _started):
        _trial_dir(current.run_dir, "trial-invalid")
        current.event(_trial_verdict("trial-invalid", valid=False))
        return WatchResult("exited", 0, None, None)

    deps = dependencies(tmp_path, watch_fn, commands, stopped)

    result = _pass_loop(session, [("task-a", "arm-a")], deps, 1)

    events = jsonl.read_rows(session.run_dir / "run-events.jsonl").rows
    finish = next(row for row in events if row["type"] == "finish")
    assert result == 3
    assert finish["exit_code"] == 3
    assert finish["t"] == 1.0


def test_nonzero_harbor_exit_with_every_trial_folder_finishes(tmp_path: Path) -> None:
    session = Session(tmp_path / "run", set_aside_limit=1)
    commands: list[list[str]] = []
    stopped: list[bool] = []
    session.event(SetAside(0.5, "earlier", "task-a", "arm-a", "upstream_errors"))

    def watch_fn(current, _process, _job_dir, _started):
        _trial_dir(current.run_dir, "trial-invalid")
        current.event(_trial_verdict("trial-invalid", valid=False))
        return WatchResult("exited", 1, None, None)

    deps = dependencies(tmp_path, watch_fn, commands, stopped)

    result = _pass_loop(session, [("task-a", "arm-a")], deps, 1)

    events = jsonl.read_rows(session.run_dir / "run-events.jsonl").rows
    assert result == 3
    assert not any(row["type"] == "halt" for row in events)


def test_harbor_failure_without_trial_folder_halts_resumably(tmp_path: Path) -> None:
    session = Session(tmp_path / "run")
    commands: list[list[str]] = []
    stopped: list[bool] = []

    def watch_fn(_current, _process, _job_dir, _started):
        return WatchResult("exited", 1, None, None)

    deps = dependencies(tmp_path, watch_fn, commands, stopped)

    result = _pass_loop(session, [("task-a", "arm-a")], deps, 0)

    events = jsonl.read_rows(session.run_dir / "run-events.jsonl").rows
    halt = next(row for row in events if row["type"] == "halt")
    assert result == 2
    assert halt["reason"] == "harbor_exit"
    assert "code 1" in halt["detail"]
    assert not any(row["type"] == "finish" for row in events)
    assert stat.S_IMODE(session.run_dir.stat().st_mode) & stat.S_IWUSR


def _launch_with_machine_check(
    tmp_path: Path,
    machine_check: MachineCheck,
    *,
    stop_launching_at: str | None = None,
    default_variant: str | None = None,
    key_env: list[str] | None = None,
    harness: str = "fake",
) -> tuple[RunOutcome, list[str | bool]]:
    job_path = tmp_path / "job.yaml"
    (tmp_path / "machine-check.json").write_text("{}", encoding="utf-8")
    started: list[str | bool] = []
    commit = "a" * 40
    fake_settings = ", fake: {}" if harness == "fake" else ""
    job_path.write_text(
        f"name: study\nmodel: provider/model\nrepetitions: 1\nconcurrency: 1\n"
        f"task_order: as_listed\ntasks: {{dataset: bench/tasks}}\n"
        f"machine_check: machine-check.json\n"
        f"arms: [{{label: arm, harness: {harness}, commit: {commit}{fake_settings}}}]\n"
        "proxy: {host: 127.0.0.1, port: 8766, container_host: host.docker.internal}\n"
        "limits: {}\n",
        encoding="utf-8",
    )
    (tmp_path / "provider").mkdir()
    (tmp_path / "provider" / "model.json").write_text(
        json.dumps(
            {
                "id": "provider/model",
                "upstream_model": "upstream-model",
                "api": "chat",
                "endpoints": [],
                "key_env": key_env or [],
                "tokens_per_minute": None,
                "pricing": {"input": 0, "output": 0, "cached_read": 0},
                "variants": {"quick": {"temperature": 0.1}} if default_variant else {},
                "withdrawn": False,
                "reason": None,
                **({"default_variant": default_variant} if default_variant else {}),
            }
        ),
        encoding="utf-8",
    )
    manifest_path = tmp_path / "manifest.toml"
    if harness == "fake":
        manifest_path.write_text("", encoding="utf-8")
    else:
        manifest_path.write_text(
            f'[{harness}."{commit}"]\n'
            'version = "fixture"\nwithdrawn = false\n'
            f'[{harness}."{commit}".images.arm64]\n'
            'ref = "hrr/opencode:test"\n'
            'digest = "sha256:' + "0" * 64 + '"\n'
            'build_method = "source"\nbuilt_at = "2026-11-15"\n',
            encoding="utf-8",
        )
    checks = CheckDeps(
        select_adapter=lambda *_args: object(),
        measure=lambda **_kwargs: (Sample(1.0, 4096, 0, 100.0, 0.0, 0, True), 0),
        inventory=lambda: Inventory(True, "ok", (), ()),
        live_run=lambda _path: None,
        host_lock=HostLock(tmp_path / "host.lock"),
        read_machine_check=lambda path: (
            machine_check if path == tmp_path / "machine-check.json" else None
        ),
        host_fingerprint=lambda: "fingerprint",
        host_profile=lambda: _PROFILE,
        harbor_version=lambda: "0.23.0",
        docker_memory_mb=lambda: 32768,
        host_memory_mb=lambda: 32768,
        bind_port=_bind_port,
        processes=lambda _run_dir: [],
        digest_of=lambda _ref: "sha256:" + "0" * 64,
        children_resolve=lambda: (True, "children resolve"),
    )
    deps = RunDeps(
        checks=checks,
        tool=ToolIdentity("0.1.0", "a" * 40),
        host=lambda: {
            "fingerprint": "fingerprint",
            "hostname": "node-a",
            "arch": "arm64",
            "cpus": 8,
            "memory_mb": 32768,
            "docker_version": "27.0.0",
            "host_profile": _PROFILE,
        },
        start_harbor=lambda *_args: started.append(True),
        watch=lambda *_args: None,
        start_services=lambda _session, restart: started.append(restart),
        stop_services=lambda _session: started.append("stop"),
        now=lambda: 1.0,
    )
    return (
        run_job(
            job_path,
            tmp_path / "runs",
            manifest_path,
            {},
            deps,
            stop_launching_at=stop_launching_at,
        ),
        started,
    )


def _machine_check_including(tasks: list[dict], included: list[str]) -> MachineCheck:
    return MachineCheck(
        created_t=1.0,
        tool_version="0.1.0",
        harbor_version="0.23.0",
        host_fingerprint="fingerprint",
        host_profile=_PROFILE,
        task_source={"dataset": "bench/tasks"},
        concurrency=1,
        tasks=tasks,
        included=included,
        verifier_timeout_multiplier=1.0,
    )


def test_run_job_records_refusal_when_another_holder_owns_host_lock(tmp_path: Path) -> None:
    holder = HostLock(tmp_path / "host.lock")
    holder.acquire()
    machine_check = _machine_check_including(
        [{"name": "task-a", "digest": "sha256:task", "memory_mb": 512}], ["task-a"]
    )
    try:
        result, started = _launch_with_machine_check(tmp_path, machine_check)
    finally:
        holder.release()

    assert result.exit_code == 1
    assert result.run_dir is not None
    events = jsonl.read_rows(result.run_dir / "run-events.jsonl").rows
    assert any(
        event["type"] == "launch_check" and event["name"] == "single_run" and event["ok"] is False
        for event in events
    )
    assert started == []


def test_run_job_refuses_empty_resolved_tasks_before_starting_services(tmp_path: Path) -> None:
    result, started = _launch_with_machine_check(tmp_path, _machine_check_including([], []))

    assert result.exit_code == 1
    assert result.run_dir is not None
    events = jsonl.read_rows(result.run_dir / "run-events.jsonl").rows
    assert any(
        event["type"] == "launch_check"
        and event["name"] == "tasks_included"
        and event["ok"] is False
        for event in events
    )
    assert started == []


def test_run_job_records_task_and_arm_metadata_and_detaches_its_log(
    tmp_path: Path,
) -> None:
    machine_check = _machine_check_including(
        [{"name": "task-a", "digest": "sha256:task", "memory_mb": 512}], ["task-a"]
    )

    result, _started = _launch_with_machine_check(tmp_path, machine_check)

    assert result.run_dir is not None
    metadata = json.loads((result.run_dir / "run-metadata.json").read_text(encoding="utf-8"))
    assert metadata["task_set"] == {
        "ref": "bench/tasks",
        "tasks": [{"name": "task-a", "digest": "sha256:task"}],
    }
    assert metadata["arms"] == [
        {
            "label": "arm",
            "harness": "fake",
            "commit": "a" * 40,
            "prompting": "direct",
            "instruction_variants_sha256": None,
            "image_ref": "",
            "image_digest": "",
            "build_method": "",
        }
    ]
    canary_config = yaml.safe_load(
        (result.run_dir / "canary-job-config.yaml").read_text(encoding="utf-8")
    )
    assert canary_config["agents"][0]["kwargs"]["manifest_path"] == str(tmp_path / "manifest.toml")
    log_path = result.run_dir / "runner.log"
    log_before = log_path.read_bytes()
    logging.getLogger("harbor_research_runner.run.loop").warning("after run")
    assert log_path.read_bytes() == log_before


def test_run_job_places_the_model_card_default_variant_in_agent_settings(
    tmp_path: Path,
) -> None:
    machine_check = _machine_check_including(
        [{"name": "task-a", "digest": "sha256:task", "memory_mb": 512}], ["task-a"]
    )

    result, _started = _launch_with_machine_check(
        tmp_path, machine_check, default_variant="quick", harness="opencode"
    )

    assert result.run_dir is not None
    canary_config = yaml.safe_load(
        (result.run_dir / "canary-job-config.yaml").read_text(encoding="utf-8")
    )
    assert canary_config["agents"][0]["kwargs"]["variant"] == "quick"


def test_run_job_uses_manifest_and_host_architecture_for_launch_checks(tmp_path: Path) -> None:
    machine_check = _machine_check_including(
        [{"name": "task-a", "digest": "sha256:task", "memory_mb": 512}], ["task-a"]
    )

    result, _started = _launch_with_machine_check(tmp_path, machine_check, harness="opencode")

    assert result.run_dir is not None
    assert (result.run_dir / "run-metadata.json").is_file()


def test_run_job_passes_environment_to_the_keys_launch_check(tmp_path: Path) -> None:
    result, _started = _launch_with_machine_check(
        tmp_path,
        _machine_check_including(
            [{"name": "task-a", "digest": "sha256:task", "memory_mb": 512}], ["task-a"]
        ),
        key_env=["MODEL_KEY"],
    )

    assert result.run_dir is not None
    events = jsonl.read_rows(result.run_dir / "run-events.jsonl").rows
    check = next(row for row in events if row.get("name") == "keys_present")
    assert check["detail"] == "unset or empty environment variable(s): MODEL_KEY"


def test_run_job_does_not_stop_services_before_they_start(tmp_path: Path) -> None:
    machine_check = _machine_check_including(
        [{"name": "task-a", "digest": "sha256:task", "memory_mb": 512}], ["task-a"]
    )

    result, service_events = _launch_with_machine_check(
        tmp_path, machine_check, stop_launching_at="not-a-time"
    )

    assert result.exit_code == 1
    assert result.run_dir is not None
    assert service_events == []


def test_run_job_records_the_unset_key_variable_launch_check(tmp_path: Path) -> None:
    result, _started = _launch_with_machine_check(
        tmp_path,
        _machine_check_including(
            [{"name": "task-a", "digest": "sha256:task", "memory_mb": 512}], ["task-a"]
        ),
        key_env=["MODEL_KEY"],
    )

    assert result.exit_code == 1
    assert result.run_dir is not None
    events = jsonl.read_rows(result.run_dir / "run-events.jsonl").rows
    key_check = next(row for row in events if row.get("name") == "keys_present")
    assert key_check["ok"] is False
    assert key_check["detail"] == "unset or empty environment variable(s): MODEL_KEY"


def _finished_session(
    tmp_path: Path, *, repetitions: int = 1, finished: bool = True
) -> tuple[Path, Path, MachineCheck]:
    job_path = tmp_path / "job.yaml"
    job_path.write_text("job", encoding="utf-8")
    machine_path = tmp_path / "machine-check.json"
    machine_path.write_text("machine", encoding="utf-8")
    manifest_path = tmp_path / "manifest.toml"
    manifest_path.write_text("", encoding="utf-8")
    job = Job(
        path=job_path,
        name="study",
        model="provider/model",
        repetitions=repetitions,
        concurrency=1,
        task_order="as_listed",
        tasks=TaskSource("dataset", "bench/tasks", None, None, None, (), ()),
        machine_check=machine_path,
        arms=(Arm("arm-a", "fake", "a" * 40),),
        proxy_host="127.0.0.1",
        proxy_port=8766,
        container_host="host.docker.internal",
        limit_overrides={},
    )
    card = ModelCard(
        id="provider/model",
        upstream_model="upstream-model",
        api="chat",
        endpoints=[],
        key_env=[],
        tokens_per_minute=None,
        pricing={"input": 0, "output": 0, "cached_read": 0},
        variants={},
        withdrawn=False,
        reason=None,
    )
    check = MachineCheck(
        created_t=1.0,
        tool_version="0.1.0",
        harbor_version="0.23.0",
        host_fingerprint="fingerprint",
        host_profile=_PROFILE,
        task_source={"dataset": "bench/tasks"},
        concurrency=1,
        tasks=[{"name": "task-a", "digest": "sha256:task", "memory_mb": 512}],
        included=["task-a"],
        verifier_timeout_multiplier=1.0,
    )
    session = RunSession.create(
        job,
        card,
        manifest_path,
        tmp_path / "runs",
        {},
        [],
        {"ref": "bench/tasks", "tasks": [{"name": "task-a", "digest": "sha256:task"}]},
        [
            {
                "label": "arm-a",
                "harness": "fake",
                "commit": "a" * 40,
                "prompting": "direct",
                "image_ref": "",
                "image_digest": "",
                "build_method": "",
            }
        ],
        check,
        lambda: 1794480000.0,
        ToolIdentity("0.1.0", "a" * 40),
        _HOST,
    )
    if finished:
        session.event(Finish(1.0, 3, 0, 1))
    return session.run_dir, manifest_path, check


def _record_canary_history(run_dir: Path, *, returncode: int, valid: bool) -> None:
    fd = jsonl.open_for_append(run_dir / "run-events.jsonl")
    try:
        jsonl.append_row(fd, HarborStart(1.0, 0, "harbor run canary", "canary").to_row())
        jsonl.append_row(fd, HarborExit(2.0, 0, returncode, "canary").to_row())
        jsonl.append_row(
            fd,
            TrialVerdict(
                t=2.0,
                trial="canary-trial",
                task="canary/say-hello",
                arm="arm-a",
                valid=valid,
                reason=None if valid else "upstream_errors",
                instrument=None if valid else "proxy",
                labels=[],
                measured=True,
                unmeasured_reason=None,
            ).to_row(),
        )
    finally:
        import os

        os.close(fd)


def _record_main_start(run_dir: Path) -> None:
    fd = jsonl.open_for_append(run_dir / "run-events.jsonl")
    try:
        jsonl.append_row(fd, HarborStart(3.0, 0, "harbor run", "main").to_row())
    finally:
        import os

        os.close(fd)


def _configure_resume_with_opencode(run_dir: Path, manifest_path: Path) -> None:
    metadata_path = run_dir / "run-metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["job"]["arms"][0]["harness"] = "opencode"
    metadata["job"]["arms"][0]["fake"] = None
    metadata["model_card"]["key_env"] = ["MODEL_KEY"]
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    commit = "a" * 40
    manifest_path.write_text(
        f'[opencode."{commit}"]\n'
        'version = "fixture"\nwithdrawn = false\n'
        f'[opencode."{commit}".images.arm64]\n'
        'ref = "hrr/opencode:test"\n'
        'digest = "sha256:' + "0" * 64 + '"\n'
        'build_method = "source"\nbuilt_at = "2026-11-15"\n',
        encoding="utf-8",
    )


def test_resume_refuses_run_stopped_after_failed_canary_without_appending_events(
    tmp_path: Path,
) -> None:
    run_dir, manifest_path, machine_check = _finished_session(tmp_path, finished=False)
    _record_canary_history(run_dir, returncode=1, valid=False)
    events_path = run_dir / "run-events.jsonl"
    before = events_path.read_bytes()
    commands: list[list[str]] = []
    deps = _resume_dependencies(
        tmp_path,
        machine_check,
        lambda *_args: WatchResult("exited", 0, None, None),
        commands,
        [],
    )

    result = resume_run(run_dir.name, run_dir.parent, manifest_path, {}, deps)

    assert result == RunOutcome(1, run_dir)
    assert events_path.read_bytes() == before
    assert run_dir.stat().st_mode & stat.S_IWUSR
    assert commands == []
    log = (run_dir / "runner.log").read_text(encoding="utf-8")
    assert run_dir.name in log
    assert "stopped before its first main pass" in log
    assert "hrr run <job file>" in log
    assert "resume failed" not in log


def test_resume_refuses_when_another_holder_owns_the_host_lock(tmp_path: Path) -> None:
    run_dir, manifest_path, machine_check = _finished_session(tmp_path, finished=False)
    holder = HostLock(tmp_path / "resume-host.lock")
    holder.acquire()
    before = (run_dir / "run-events.jsonl").read_bytes()
    deps = _resume_dependencies(tmp_path, machine_check, lambda *_args: None, [], [])

    try:
        result = resume_run(run_dir.name, run_dir.parent, manifest_path, {}, deps)
    finally:
        holder.release()

    assert result == RunOutcome(1, None)
    assert (run_dir / "run-events.jsonl").read_bytes() == before
    assert not (run_dir / "runner.log").exists()


def test_resume_repairs_a_set_aside_move_without_its_event(tmp_path: Path) -> None:
    run_dir, manifest_path, machine_check = _finished_session(tmp_path, finished=False)
    _record_main_start(run_dir)
    (run_dir / "harbor-job").mkdir()
    _trial_dir(run_dir, "trial-complete")
    stranded = run_dir / "set-aside-trials" / "trial-old"
    stranded.mkdir(parents=True)
    fd = jsonl.open_for_append(run_dir / "run-events.jsonl")
    try:
        jsonl.append_row(fd, _trial_verdict("trial-old", valid=False).to_row())
        jsonl.append_row(fd, _trial_verdict("trial-complete", valid=True).to_row())
    finally:
        import os

        os.close(fd)
    deps = _resume_dependencies(
        tmp_path,
        machine_check,
        lambda *_args: WatchResult("exited", 0, None, None),
        [],
        [],
    )
    stopped_run = tmp_path / "stopped-run"

    def stop_services(session) -> None:
        stopped_run.write_text("none" if session is None else session.run_dir.name)

    deps = replace(
        deps,
        start_services=lambda *_args: None,
        stop_services=stop_services,
    )

    result = resume_run(run_dir.name, run_dir.parent, manifest_path, {}, deps)

    events = jsonl.read_rows(run_dir / "run-events.jsonl").rows
    repaired = [row for row in events if row.get("type") == "set_aside"]
    assert repaired == [
        {
            "type": "set_aside",
            "t": 2.0,
            "trial": "trial-old",
            "task": "task-a",
            "arm": "arm-a",
            "reason": "upstream_errors",
        }
    ]
    assert result.exit_code == 0
    assert result.run_dir == run_dir
    assert stopped_run.read_text(encoding="utf-8") == run_dir.name


def test_resume_refuses_run_stopped_after_passed_canary_without_appending_events(
    tmp_path: Path,
) -> None:
    run_dir, manifest_path, machine_check = _finished_session(tmp_path, finished=False)
    _record_canary_history(run_dir, returncode=0, valid=True)
    events_path = run_dir / "run-events.jsonl"
    before = events_path.read_bytes()
    commands: list[list[str]] = []
    deps = _resume_dependencies(
        tmp_path,
        machine_check,
        lambda *_args: WatchResult("exited", 0, None, None),
        commands,
        [],
    )

    result = resume_run(run_dir.name, run_dir.parent, manifest_path, {}, deps)

    assert result == RunOutcome(1, run_dir)
    assert events_path.read_bytes() == before
    assert run_dir.stat().st_mode & stat.S_IWUSR
    assert commands == []
    log = (run_dir / "runner.log").read_text(encoding="utf-8")
    assert run_dir.name in log
    assert "stopped before its first main pass" in log
    assert "hrr run <job file>" in log
    assert "resume failed" not in log


def test_resume_of_finished_run_returns_one_without_appending(tmp_path: Path) -> None:
    run_dir, manifest_path, _check = _finished_session(tmp_path)
    before = (run_dir / "run-events.jsonl").read_bytes()
    commands: list[list[str]] = []
    stopped: list[bool] = []
    deps = dependencies(tmp_path, lambda *_args: None, commands, stopped)

    result = resume_run(run_dir.name, run_dir.parent, manifest_path, {}, deps)

    assert result == RunOutcome(1, run_dir)
    assert (run_dir / "run-events.jsonl").read_bytes() == before
    assert commands == []
    log = (run_dir / "runner.log").read_text(encoding="utf-8")
    assert "already finished" in log
    assert "resume failed" not in log
    assert stopped == []


def test_resume_detects_finish_at_the_end_of_the_event_log(tmp_path: Path) -> None:
    run_dir, manifest_path, machine_check = _finished_session(tmp_path, finished=False)
    _record_main_start(run_dir)
    session = RunSession.load(run_dir, {})
    session.event(LaunchCheck(1.0, "earlier", True, "ok"))
    session.event(Finish(2.0, 3, 0, 1))
    before = (run_dir / "run-events.jsonl").read_bytes()
    stopped: list[bool] = []
    deps = _resume_dependencies(
        tmp_path,
        machine_check,
        lambda *_args: WatchResult("exited", 0, None, None),
        [],
        stopped,
    )

    result = resume_run(run_dir.name, run_dir.parent, manifest_path, {}, deps)

    assert result == RunOutcome(1, run_dir)
    assert (run_dir / "run-events.jsonl").read_bytes() == before
    assert stopped == []


def test_resume_refuses_a_missing_run_folder_without_leaking_cleanup_state(
    tmp_path: Path,
) -> None:
    _run_dir, manifest_path, machine_check = _finished_session(tmp_path, finished=False)
    stopped: list[bool] = []
    deps = _resume_dependencies(
        tmp_path,
        machine_check,
        lambda *_args: WatchResult("exited", 0, None, None),
        [],
        stopped,
    )

    result = resume_run("missing-run", tmp_path / "runs", manifest_path, {}, deps)

    assert result == RunOutcome(1, None)
    assert stopped == []


def test_resume_returns_launch_check_failure_with_run_folder(tmp_path: Path) -> None:
    run_dir, manifest_path, machine_check = _finished_session(tmp_path, finished=False)
    _record_main_start(run_dir)
    events_path = run_dir / "run-events.jsonl"
    before = events_path.read_bytes()
    stopped: list[bool] = []
    deps = _resume_dependencies(
        tmp_path,
        machine_check,
        lambda *_args: WatchResult("exited", 0, None, None),
        [],
        stopped,
    )
    deps = replace(
        deps,
        checks=replace(deps.checks, processes=lambda _path: [987654321]),
    )

    result = resume_run(run_dir.name, run_dir.parent, manifest_path, {}, deps)

    assert result == RunOutcome(1, run_dir)
    assert events_path.read_bytes() == before
    assert stopped == []
    assert "resume failed" not in (run_dir / "runner.log").read_text(encoding="utf-8")


def test_resume_stops_services_when_resume_event_write_fails(tmp_path: Path) -> None:
    run_dir, manifest_path, machine_check = _finished_session(tmp_path, finished=False)
    _record_main_start(run_dir)
    stopped: list[bool] = []
    services_started = False

    def now() -> float:
        if services_started:
            raise RuntimeError("clock failed")
        return 2.0

    def start_services(session, _restart: bool) -> None:
        nonlocal services_started
        session._lifeline_read_fd = 0
        services_started = True

    deps = _resume_dependencies(
        tmp_path,
        machine_check,
        lambda *_args: WatchResult("exited", 0, None, None),
        [],
        stopped,
    )
    deps = replace(deps, start_services=start_services, now=now)

    result = resume_run(run_dir.name, run_dir.parent, manifest_path, {}, deps)

    assert result == RunOutcome(1, run_dir)
    assert stopped == [True]


def test_resume_finishes_when_every_slot_has_a_valid_trial(tmp_path: Path) -> None:
    run_dir, manifest_path, machine_check = _finished_session(tmp_path, finished=False)
    (run_dir / "harbor-job").mkdir()
    _record_main_start(run_dir)
    _trial_dir(run_dir, "trial-valid")
    fd = jsonl.open_for_append(run_dir / "run-events.jsonl")
    try:
        jsonl.append_row(fd, _trial_verdict("trial-valid", valid=True).to_row())
    finally:
        import os

        os.close(fd)
    stopped: list[bool] = []
    commands: list[list[str]] = []
    deps = _resume_dependencies(
        tmp_path,
        machine_check,
        lambda *_args: WatchResult("exited", 0, None, None),
        commands,
        stopped,
    )
    deps = replace(
        deps,
        start_services=lambda *_args: None,
        stop_services=lambda _session: stopped.append(True),
    )

    result = resume_run(run_dir.name, run_dir.parent, manifest_path, {}, deps)

    events = jsonl.read_rows(run_dir / "run-events.jsonl").rows
    finish = next(row for row in events if row["type"] == "finish")
    assert result == RunOutcome(0, run_dir)
    assert commands == []
    assert stopped == [True]
    assert finish["slots_complete"] == 1
    assert finish["slots_incomplete"] == 0


def test_resume_uses_manifest_architecture_and_environment_for_launch_checks(
    tmp_path: Path,
) -> None:
    run_dir, manifest_path, machine_check = _finished_session(tmp_path, finished=False)
    _configure_resume_with_opencode(run_dir, manifest_path)
    (run_dir / "harbor-job").mkdir(exist_ok=True)
    _record_main_start(run_dir)
    _trial_dir(run_dir, "trial-valid")
    fd = jsonl.open_for_append(run_dir / "run-events.jsonl")
    try:
        jsonl.append_row(fd, _trial_verdict("trial-valid", valid=True).to_row())
    finally:
        import os

        os.close(fd)
    deps = _resume_dependencies(tmp_path, machine_check, lambda *_args: None, [], [])
    deps = replace(
        deps,
        checks=replace(deps.checks, digest_of=lambda _ref: "sha256:" + "0" * 64),
        start_services=lambda session, _restart: setattr(session, "_lifeline_read_fd", 0),
        stop_services=lambda _session: None,
    )

    result = resume_run(
        run_dir.name,
        run_dir.parent,
        manifest_path,
        {"MODEL_KEY": "configured"},
        deps,
    )

    assert result == RunOutcome(0, run_dir)


def test_resume_sets_aside_invalid_trial_before_next_pass(tmp_path: Path) -> None:
    run_dir, manifest_path, machine_check = _finished_session(tmp_path, finished=False)
    (run_dir / "harbor-job").mkdir()
    _record_main_start(run_dir)
    _trial_dir(run_dir, "trial-invalid")
    fd = jsonl.open_for_append(run_dir / "run-events.jsonl")
    try:
        jsonl.append_row(fd, _trial_verdict("trial-invalid", valid=False).to_row())
    finally:
        import os

        os.close(fd)
    commands: list[list[str]] = []
    deps = _resume_dependencies(
        tmp_path,
        machine_check,
        lambda *_args: WatchResult("exited", 0, None, None),
        commands,
        [],
    )
    deps = replace(deps, start_services=lambda *_args: None)

    result = resume_run(run_dir.name, run_dir.parent, manifest_path, {}, deps)

    events = jsonl.read_rows(run_dir / "run-events.jsonl").rows
    set_aside = next(row for row in events if row["type"] == "set_aside")
    assert result == RunOutcome(1, run_dir)
    assert commands == []
    assert not (run_dir / "harbor-job" / "trial-invalid").exists()
    assert (run_dir / "set-aside-trials" / "trial-invalid").is_dir()
    assert set_aside == {
        "type": "set_aside",
        "t": 2.0,
        "trial": "trial-invalid",
        "task": "task-a",
        "arm": "arm-a",
        "reason": "upstream_errors",
    }


def test_resume_on_a_different_node_restarts_harbor_when_a_slot_has_unplanned_trials(
    tmp_path: Path,
) -> None:
    run_dir, manifest_path, machine_check = _finished_session(
        tmp_path, repetitions=2, finished=False
    )
    trial_dir = run_dir / "harbor-job" / "trial-existing"
    trial_dir.mkdir(parents=True)
    (trial_dir / "config.json").write_text("{}", encoding="utf-8")
    events_path = run_dir / "run-events.jsonl"
    fd = jsonl.open_for_append(events_path)
    try:
        jsonl.append_row(fd, _trial_verdict("trial-existing", valid=True).to_row())
        jsonl.append_row(fd, HarborStart(1.0, 0, "harbor run", "main").to_row())
        jsonl.append_row(fd, HarborExit(1.0, 0, -15, "main").to_row())
        jsonl.append_row(fd, Halt(1.0, "quota_exhausted", "halted during pass").to_row())
    finally:
        import os

        os.close(fd)

    commands: list[list[str]] = []
    stopped: list[bool] = []
    observed_deadlines: list[tuple[str | None, float | None]] = []

    def watch_fn(current, _process, _job_dir, _started):
        observed_deadlines.append((current.stop_launching_at, current.stop_launching_at_epoch))
        new_trial = run_dir / "harbor-job" / "trial-resumed"
        new_trial.mkdir()
        (new_trial / "config.json").write_text("{}", encoding="utf-8")
        current.event(_trial_verdict("trial-resumed", valid=True))
        return WatchResult("exited", 0, None, None)

    deps = _resume_dependencies(
        tmp_path, machine_check, watch_fn, commands, stopped, hostname="node-b"
    )
    deps = replace(deps, tool=ToolIdentity("0.1.1", "b" * 40))
    metadata_before = (run_dir / "run-metadata.json").read_bytes()

    stop_launching_at = "2026-12-01T10:00:00+00:00"
    result = resume_run(
        run_dir.name,
        run_dir.parent,
        manifest_path,
        {},
        deps,
        stop_launching_at=stop_launching_at,
    )

    events = jsonl.read_rows(events_path).rows
    resume_event = next(row for row in events if row.get("type") == "resume")
    start_index = next(
        index
        for index, row in enumerate(events)
        if row.get("type") == "harbor_start" and row.get("pass") == 1
    )
    finish_index = next(index for index, row in enumerate(events) if row.get("type") == "finish")
    assert result == RunOutcome(0, run_dir)
    assert resume_event["tool_version"] == "0.1.1"
    assert resume_event["tool_commit"] == "b" * 40
    assert (run_dir / "run-metadata.json").read_bytes() == metadata_before
    assert commands[0][1:3] == ["jobs", "resume"]
    assert commands[0][-2:] == ["--pk", f"stop_launching_at={stop_launching_at}"]
    assert len(observed_deadlines) == 1
    assert observed_deadlines[0][0] == stop_launching_at
    assert observed_deadlines[0][1] is not None
    assert any(
        row.get("type") == "stop_launching_at" and row.get("time") == stop_launching_at
        for row in events
    )
    assert start_index < finish_index


def test_resume_numbers_the_next_pass_after_all_previous_main_passes(tmp_path: Path) -> None:
    run_dir, manifest_path, machine_check = _finished_session(
        tmp_path, repetitions=2, finished=False
    )
    trial_dir = run_dir / "harbor-job" / "trial-existing"
    trial_dir.mkdir(parents=True)
    (trial_dir / "config.json").write_text("{}", encoding="utf-8")
    events_path = run_dir / "run-events.jsonl"
    fd = jsonl.open_for_append(events_path)
    try:
        jsonl.append_row(fd, _trial_verdict("trial-existing", valid=True).to_row())
        for pass_number in range(3):
            jsonl.append_row(
                fd,
                HarborStart(1.0, pass_number, "harbor run", "main").to_row(),
            )
            jsonl.append_row(
                fd,
                HarborExit(1.0, pass_number, -15, "main").to_row(),
            )
    finally:
        import os

        os.close(fd)

    def watch_fn(current, _process, _job_dir, _started):
        new_trial = run_dir / "harbor-job" / "trial-resumed"
        new_trial.mkdir()
        (new_trial / "config.json").write_text("{}", encoding="utf-8")
        current.event(_trial_verdict("trial-resumed", valid=True))
        return WatchResult("exited", 0, None, None)

    commands: list[list[str]] = []
    deps = _resume_dependencies(tmp_path, machine_check, watch_fn, commands, [])

    result = resume_run(run_dir.name, run_dir.parent, manifest_path, {}, deps)

    events = jsonl.read_rows(events_path).rows
    starts = [
        row for row in events if row.get("type") == "harbor_start" and row.get("job") == "main"
    ]
    assert result == RunOutcome(0, run_dir)
    assert starts[-1]["pass"] == 3


def test_create_uses_a_utc_microsecond_run_folder_name(tmp_path: Path) -> None:
    session = _create(tmp_path)

    assert session.run_dir.name == "study-20261112T104000123456Z"


def test_create_preserves_a_run_folder_that_already_exists(tmp_path: Path) -> None:
    session = _create(tmp_path)
    events_path = session.run_dir / "run-events.jsonl"
    before = events_path.read_bytes()

    with pytest.raises(FileExistsError):
        _create(tmp_path)

    assert events_path.read_bytes() == before


def test_create_uses_the_machine_check_verifier_multiplier(tmp_path: Path) -> None:
    _job, _card, _manifest_path, machine_check = _inputs(tmp_path)
    machine_check = replace(machine_check, verifier_timeout_multiplier=2.5)

    session = _create(tmp_path, machine_check=machine_check)

    assert session.limits.verifier_timeout_multiplier == 2.5


def test_resume_refuses_when_host_profile_memory_cap_differs(tmp_path: Path) -> None:
    run_dir, manifest_path, machine_check = _finished_session(tmp_path, finished=False)
    _record_main_start(run_dir)
    changed_profile = {**_PROFILE, "memory_cap": "none"}
    deps = _resume_dependencies(
        tmp_path,
        machine_check,
        lambda *_args: None,
        [],
        [],
        profile=changed_profile,
    )
    before = (run_dir / "run-events.jsonl").read_bytes()

    result = resume_run(run_dir.name, run_dir.parent, manifest_path, {}, deps)

    assert result == RunOutcome(1, run_dir)
    assert (run_dir / "run-events.jsonl").read_bytes() == before
    assert "memory_cap differs" in (run_dir / "runner.log").read_text(encoding="utf-8")
    assert "resume refused because the host profile differs" in (run_dir / "runner.log").read_text(
        encoding="utf-8"
    )
    assert "resume failed" not in (run_dir / "runner.log").read_text(encoding="utf-8")


_HOST = {
    "fingerprint": "fingerprint",
    "hostname": "node-a",
    "arch": "arm64",
    "cpus": 8,
    "memory_mb": 32768,
    "docker_version": "27.0.0",
    "host_profile": _PROFILE,
}


def _inputs(tmp_path: Path):
    job_path = tmp_path / "job.yaml"
    job_path.write_text(
        "name: study\nmodel: provider/model\nrepetitions: 1\nconcurrency: 1\n"
        "task_order: as_listed\ntasks:\n  dataset: bench/tasks\n"
        "machine_check: machine-check.json\narms:\n  - {label: arm-a, harness: fake, commit: "
        + "'"
        + "a" * 40
        + "'}\nproxy: {host: 127.0.0.1, port: 8766, container_host: host.docker.internal}\n"
        "limits: {}\n",
        encoding="utf-8",
    )
    machine_check_path = tmp_path / "machine-check.json"
    machine_check_path.write_text("{}", encoding="utf-8")
    manifest_path = tmp_path / "harness-manifest.toml"
    manifest_path.write_text("", encoding="utf-8")
    job = Job(
        path=job_path,
        name="study",
        model="provider/model",
        repetitions=1,
        concurrency=1,
        task_order="as_listed",
        tasks=TaskSource("dataset", "bench/tasks", None, None, None, (), ()),
        machine_check=machine_check_path,
        arms=(Arm("arm-a", "fake", "a" * 40),),
        proxy_host="127.0.0.1",
        proxy_port=8766,
        container_host="host.docker.internal",
        limit_overrides={},
    )
    card = ModelCard(
        id="provider/model",
        upstream_model="upstream-model",
        api="chat",
        endpoints=["https://model.invalid/v1"],
        key_env=["MODEL_KEY"],
        tokens_per_minute=None,
        pricing={"input": 0, "output": 0, "cached_read": 0},
        variants={},
        withdrawn=False,
        reason=None,
    )
    machine_check = MachineCheck(
        created_t=1,
        tool_version="0.1.0",
        harbor_version="0.23.0",
        host_fingerprint="fingerprint",
        host_profile=_PROFILE,
        task_source={"dataset": "bench/tasks"},
        concurrency=1,
        tasks=[{"name": "task-a", "digest": "sha256:task", "memory_mb": 512}],
        included=["task-a"],
        verifier_timeout_multiplier=1.0,
    )
    return job, card, manifest_path, machine_check


def _create(
    tmp_path: Path,
    check_results: list[CheckResult] | None = None,
    job: Job | None = None,
    host: dict | None = None,
    machine_check: MachineCheck | None = None,
) -> RunSession:
    default_job, card, manifest_path, default_machine_check = _inputs(tmp_path)
    job = job or default_job
    return RunSession.create(
        job=job,
        card=card,
        manifest_path=manifest_path,
        runs_dir=tmp_path / "runs",
        environment={"MODEL_KEY": "configured"},
        check_results=check_results or [CheckResult("ready", True, "ok")],
        tasks={"ref": "bench/tasks", "tasks": [{"name": "task-a", "digest": "sha256:task"}]},
        arms=[
            {
                "label": "arm-a",
                "harness": "fake",
                "commit": "a" * 40,
                "prompting": "direct",
                "image_ref": "",
                "image_digest": "",
                "build_method": "",
            }
        ],
        machine_check=machine_check or default_machine_check,
        now=lambda: 1794480000.123456,
        tool=ToolIdentity("0.1.0", "a" * 40),
        host=_HOST if host is None else host,
    )


def test_create_writes_launch_checks_and_metadata(tmp_path: Path) -> None:
    session = _create(tmp_path)

    events = [
        json.loads(line) for line in (session.run_dir / "run-events.jsonl").read_text().splitlines()
    ]
    metadata = json.loads((session.run_dir / "run-metadata.json").read_text())
    assert [event["type"] for event in events] == ["launch", "launch_check"]
    assert events[0]["t"] == 1794480000.123456
    assert events[0]["tool_version"] == "0.1.0"
    assert events[0]["tool_commit"] == "a" * 40
    assert events[0]["pid"] == os.getpid()
    assert events[1]["name"] == "ready"
    assert metadata["host"] == _HOST
    assert metadata["tool_version"] == "0.1.0"
    assert metadata["harbor_version"] == "0.23.0"
    assert session.manifest_path == tmp_path / "harness-manifest.toml"


def test_create_records_job_arm_task_and_proxy_metadata(tmp_path: Path) -> None:
    session = _create(tmp_path)

    metadata = json.loads((session.run_dir / "run-metadata.json").read_text(encoding="utf-8"))

    assert metadata["run_id"] == session.run_dir.name
    assert metadata["created_t"] == 1794480000.123456
    assert metadata["job"]["path"] == str(session.job.path)
    assert metadata["job"]["machine_check"] == str(session.job.machine_check)
    assert metadata["job_file_sha256"] == hashlib.sha256(session.job.path.read_bytes()).hexdigest()
    assert metadata["arms"] == [
        {
            "label": "arm-a",
            "harness": "fake",
            "commit": "a" * 40,
            "prompting": "direct",
            "instruction_variants_sha256": None,
            "image_ref": "",
            "image_digest": "",
            "build_method": "",
        }
    ]
    assert metadata["task_set"] == {
        "ref": "bench/tasks",
        "tasks": [{"name": "task-a", "digest": "sha256:task"}],
    }
    assert metadata["proxy"] == {"host": "127.0.0.1", "port": 8766}
    assert metadata["sampler_interval_s"] == 10.0


def test_create_keeps_the_checked_machine_check_in_the_run_folder(tmp_path: Path) -> None:
    session = _create(tmp_path)
    source = session.job.machine_check
    copy = session.run_dir / "machine-check.json"

    source.write_text('{"changed": true}', encoding="utf-8")

    metadata = json.loads((session.run_dir / "run-metadata.json").read_text())
    assert copy.read_bytes() == b"{}"
    assert hashlib.sha256(copy.read_bytes()).hexdigest() == metadata["machine_check_sha256"]


def test_create_refuses_failed_checks_without_metadata(tmp_path: Path) -> None:
    with pytest.raises(RefusedToStart):
        _create(tmp_path, [CheckResult("ready", False, "not ready")])

    run_dir = next((tmp_path / "runs").iterdir())
    events = [json.loads(line) for line in (run_dir / "run-events.jsonl").read_text().splitlines()]
    assert events[-1] == {
        "type": "launch_check",
        "t": 1794480000.123456,
        "name": "ready",
        "ok": False,
        "detail": "not ready",
    }
    assert not (run_dir / "run-metadata.json").exists()


def test_create_refuses_a_host_profile_with_missing_fields(tmp_path: Path) -> None:
    host = {key: value for key, value in _HOST.items() if key != "hostname"}

    with pytest.raises(ValueError, match=r"missing=\['hostname'\], extra=\[\]"):
        _create(tmp_path, host=host)

    assert not (tmp_path / "runs").exists()


def test_create_refuses_a_host_profile_with_extra_fields(tmp_path: Path) -> None:
    host = {**_HOST, "unexpected": "value"}

    with pytest.raises(ValueError, match=r"missing=\[\], extra=\['unexpected'\]"):
        _create(tmp_path, host=host)

    assert not (tmp_path / "runs").exists()


def test_create_leaves_stop_deadline_fields_unset(tmp_path: Path) -> None:
    session = _create(tmp_path)

    assert session.stop_launching_at is None
    assert session.stop_launching_at_epoch is None


def test_load_does_not_change_metadata_or_append_event(tmp_path: Path) -> None:
    session = _create(tmp_path)
    metadata_path = session.run_dir / "run-metadata.json"
    before = metadata_path.read_bytes()
    events_before = (session.run_dir / "run-events.jsonl").read_bytes()

    loaded = RunSession.load(session.run_dir, {"MODEL_KEY": "configured"})

    assert loaded.run_dir == session.run_dir
    assert metadata_path.read_bytes() == before
    assert (session.run_dir / "run-events.jsonl").read_bytes() == events_before


def test_load_preserves_the_job_file_paths(tmp_path: Path) -> None:
    session = _create(tmp_path)

    loaded = RunSession.load(session.run_dir, {"MODEL_KEY": "configured"})

    assert loaded.job.path == session.job.path
    assert loaded.job.machine_check == session.job.machine_check
    assert loaded.tool == ToolIdentity("0.1.0", "a" * 40)


def test_stopping_an_unstarted_session_is_safe(tmp_path: Path) -> None:
    session = _create(tmp_path)

    session.stop_services()

    assert session.proxy_alive() is False
    assert session.oom_watch_alive() is False
    assert session.memory_watch_alive() is False


def test_resume_keeps_absolute_task_path_from_stored_job(tmp_path: Path) -> None:
    study = tmp_path / "a"
    study.mkdir()
    task_path = tmp_path / "tasks"
    (task_path / "example-task").mkdir(parents=True)
    job_path = study / "job.yaml"
    job_path.write_text(
        "name: study\nmodel: provider/model\nrepetitions: 1\nconcurrency: 1\n"
        "task_order: as_listed\ntasks: {path: ../tasks}\n"
        "machine_check: ../machine-check.json\n"
        "arms: [{label: arm-a, harness: fake, commit: " + "a" * 40 + "}]\n"
        "proxy: {host: 127.0.0.1, port: 8766, container_host: host.docker.internal}\n"
        "limits: {}\n",
        encoding="utf-8",
    )
    parsed_job = load_job(job_path)
    session = _create(tmp_path, job=parsed_job)

    resumed = RunSession.load(session.run_dir, {"MODEL_KEY": "configured"})

    assert resumed.job.tasks.path == str(task_path.resolve())


def test_mark_resumed_appends_resume_event(tmp_path: Path) -> None:
    session = _create(tmp_path)
    session.tool = ToolIdentity("0.1.0", "a" * 40, "v0.1.0")

    session.mark_resumed(lambda: 1794480100.5)

    event = json.loads((session.run_dir / "run-events.jsonl").read_text().splitlines()[-1])
    assert event == {
        "type": "resume",
        "t": 1794480100.5,
        "tool_version": "0.1.0",
        "tool_commit": "a" * 40,
        "tool_revision": "v0.1.0",
        "pid": __import__("os").getpid(),
    }


def test_resume_refuses_a_run_that_predates_host_profile_recording(tmp_path: Path) -> None:
    run_dir, manifest_path, machine_check = _finished_session(tmp_path, finished=False)
    _record_main_start(run_dir)
    metadata_path = run_dir / "run-metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    del metadata["host"]["host_profile"]
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    deps = _resume_dependencies(tmp_path, machine_check, lambda *_args: None, [], [])
    before = (run_dir / "run-events.jsonl").read_bytes()

    result = resume_run(run_dir.name, run_dir.parent, manifest_path, {}, deps)

    assert result == RunOutcome(1, run_dir)
    assert (run_dir / "run-events.jsonl").read_bytes() == before
    log = (run_dir / "runner.log").read_text(encoding="utf-8")
    assert "predates host profile recording" in log
    assert "resume failed" not in log
