from __future__ import annotations

import stat
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

from harbor_research_runner.docker_host.cleanup import Inventory
from harbor_research_runner.docker_host.host_lock import HostLock
from harbor_research_runner.job_config.job_file import Arm, Job, TaskSource
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
    SetAside,
    TrialVerdict,
)
from harbor_research_runner.run.checks import CheckDeps
from harbor_research_runner.run.loop import (
    RunDeps,
    RunSession,
    _pass_loop,
    make_read_only,
    resume_run,
    slots_status,
)
from harbor_research_runner.run.tool_identity import ToolIdentity
from harbor_research_runner.run.watch import WatchResult


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
    return RunDeps(
        checks=CheckDeps(
            select_adapter=lambda *_args: None,
            measure=lambda: None,
            inventory=lambda: None,
            live_run=lambda _path: None,
            host_lock=HostLock(tmp_path / "host.lock"),
            read_machine_check=lambda _path: None,
            host_fingerprint=lambda: "fingerprint",
            harbor_version=lambda: "0.23.0",
        ),
        tool=ToolIdentity("0.1.0", "a" * 40),
        host=lambda: {},
        start_harbor=lambda argv, _cwd: commands.append(argv) or Process(),
        watch=watch_fn,
        start_services=lambda _session, _restart: None,
        stop_services=lambda _session: stopped.append(True),
        now=lambda: 1.0,
    )


def _resume_dependencies(
    tmp_path: Path,
    machine_check: MachineCheck,
    watch_fn: Callable,
    commands: list[list[str]],
    stopped: list[bool],
) -> RunDeps:
    return RunDeps(
        checks=CheckDeps(
            select_adapter=lambda *_args: object(),
            measure=lambda: Sample(1.0, 4096, 0, 100.0, 0.0, 0, True),
            inventory=lambda: Inventory(True, "ok", (), ()),
            live_run=lambda _path: None,
            host_lock=HostLock(tmp_path / "resume-host.lock"),
            read_machine_check=lambda _path: machine_check,
            host_fingerprint=lambda: "fingerprint",
            harbor_version=lambda: "0.23.0",
        ),
        tool=ToolIdentity("0.1.0", "a" * 40),
        host=lambda: {
            "fingerprint": "fingerprint",
            "arch": "arm64",
            "cpus": 8,
            "memory_mb": 32768,
            "docker_version": "27.0.0",
        },
        start_harbor=lambda argv, _cwd: commands.append(argv) or Process(),
        watch=watch_fn,
        start_services=lambda _session, _restart: None,
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
    assert commands[0][3] == "run"
    assert commands[1][3:5] == ["jobs", "resume"]
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
        task_source={"dataset": "bench/tasks"},
        concurrency=1,
        tasks=[{"name": "task-a", "digest": "sha256:task"}],
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
                "image_ref": "",
                "image_digest": "",
                "build_method": "",
            }
        ],
        check,
        lambda: 1794480000.0,
        ToolIdentity("0.1.0", "a" * 40),
        {
            "fingerprint": "fingerprint",
            "arch": "arm64",
            "cpus": 8,
            "memory_mb": 32768,
            "docker_version": "27.0.0",
        },
    )
    if finished:
        session.event(Finish(1.0, 3, 0, 1))
    return session.run_dir, manifest_path, check


def test_resume_of_finished_run_returns_one_without_appending(tmp_path: Path) -> None:
    run_dir, manifest_path, _check = _finished_session(tmp_path)
    before = (run_dir / "run-events.jsonl").read_bytes()
    commands: list[list[str]] = []
    stopped: list[bool] = []
    deps = dependencies(tmp_path, lambda *_args: None, commands, stopped)

    result = resume_run(run_dir.name, run_dir.parent, manifest_path, {}, deps)

    assert result == 1
    assert (run_dir / "run-events.jsonl").read_bytes() == before
    assert commands == []


def test_resume_restarts_harbor_when_a_slot_has_unplanned_trials(
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

    def watch_fn(current, _process, _job_dir, _started):
        new_trial = run_dir / "harbor-job" / "trial-resumed"
        new_trial.mkdir()
        (new_trial / "config.json").write_text("{}", encoding="utf-8")
        current.event(_trial_verdict("trial-resumed", valid=True))
        return WatchResult("exited", 0, None, None)

    deps = _resume_dependencies(tmp_path, machine_check, watch_fn, commands, stopped)

    result = resume_run(run_dir.name, run_dir.parent, manifest_path, {}, deps)

    events = jsonl.read_rows(events_path).rows
    start_index = next(
        index
        for index, row in enumerate(events)
        if row.get("type") == "harbor_start" and row.get("pass") == 1
    )
    finish_index = next(index for index, row in enumerate(events) if row.get("type") == "finish")
    assert result == 0
    assert commands[0][3:5] == ["jobs", "resume"]
    assert start_index < finish_index
