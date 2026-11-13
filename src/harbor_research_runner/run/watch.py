from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

from harbor_research_runner.harbor_adapter import job_folder, set_aside
from harbor_research_runner.records import jsonl, machine_check
from harbor_research_runner.records.jsonl import JsonlTail
from harbor_research_runner.records.run_events import Halt, TrialVerdict
from harbor_research_runner.run import process_group
from harbor_research_runner.trial_validity import run_health, verdict


@dataclass(frozen=True)
class WatchResult:
    kind: str
    returncode: int | None
    halt_reason: str | None
    halt_detail: str | None


def _read_events(run_dir: Path) -> list[dict]:
    return jsonl.read_rows(run_dir / "run-events.jsonl").rows


def _trial_verdict(
    session,
    trial_dir: Path,
    now: float,
    ledger_tail: JsonlTail,
    resource_tail: JsonlTail,
) -> dict:
    facts = job_folder.read_trial(trial_dir)
    ledger = ledger_tail.read()
    resources = resource_tail.read()
    oom_path = session.run_dir / "docker-oom-events.jsonl"
    oom_events = jsonl.read_rows(oom_path).rows if oom_path.exists() else []
    events = _read_events(session.run_dir)
    earlier = set_aside.earlier_reasons(events, facts.task, facts.arm)
    result = verdict.judge_trial(
        facts,
        ledger.rows,
        ledger,
        resources.rows,
        session.limits,
        earlier,
        oom_events,
        jsonl.read_rows(session.run_dir / "memory-watch.jsonl").rows
        if (session.run_dir / "memory-watch.jsonl").exists()
        else [],
    )
    event = TrialVerdict(
        t=now,
        trial=facts.trial,
        task=facts.task,
        arm=facts.arm,
        valid=result.valid,
        reason=result.reason,
        instrument=result.instrument,
        labels=list(result.labels),
        measured=result.measured,
        unmeasured_reason=result.unmeasured_reason,
        oom_source=result.oom_source,
    )
    session.event(event)
    return event.to_row()


def _result_parses(trial_dir: Path) -> bool:
    try:
        value = json.loads((trial_dir / "result.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    return isinstance(value, dict)


def judge_finished(session, job_dir: Path, judged: set[str], now: float) -> list[str]:
    return _judge_finished(
        session,
        job_dir,
        judged,
        now,
        JsonlTail(session.run_dir / "proxy-ledger.jsonl"),
        JsonlTail(session.run_dir / "resource-stats.jsonl"),
    )


def _judge_finished(
    session,
    job_dir: Path,
    judged: set[str],
    now: float,
    ledger_tail: JsonlTail,
    resource_tail: JsonlTail,
) -> list[str]:
    newly_judged = []
    for trial_dir in job_folder.trial_folders(job_dir):
        if trial_dir.name in judged or not _result_parses(trial_dir):
            continue
        _trial_verdict(session, trial_dir, now, ledger_tail, resource_tail)
        judged.add(trial_dir.name)
        newly_judged.append(trial_dir.name)
    return newly_judged


def judge_all_remaining(session, job_dir: Path, judged: set[str], now: float) -> list[str]:
    return _judge_all_remaining(
        session,
        job_dir,
        judged,
        now,
        JsonlTail(session.run_dir / "proxy-ledger.jsonl"),
        JsonlTail(session.run_dir / "resource-stats.jsonl"),
    )


def _judge_all_remaining(
    session,
    job_dir: Path,
    judged: set[str],
    now: float,
    ledger_tail: JsonlTail,
    resource_tail: JsonlTail,
) -> list[str]:
    newly_judged = []
    for trial_dir in job_folder.trial_folders(job_dir):
        if trial_dir.name in judged:
            continue
        _trial_verdict(session, trial_dir, now, ledger_tail, resource_tail)
        judged.add(trial_dir.name)
        newly_judged.append(trial_dir.name)
    return newly_judged


def _task_budgets(session) -> tuple[float, float]:
    check = machine_check.read(session.job.machine_check)
    task_set = session.metadata.task_set
    selected = [row["name"] for row in task_set["tasks"]]
    if not selected:
        raise ValueError("machine check task selection is empty")
    task_rows = {row["name"]: row for row in check.tasks if isinstance(row.get("name"), str)}
    agent_budgets = []
    verifier_budgets = []
    for name in selected:
        row = task_rows.get(name)
        if row is None:
            raise ValueError(f"machine check has no budget row for task {name!r}")
        agent_budget = row.get("agent_budget_s")
        if isinstance(agent_budget, bool) or not isinstance(agent_budget, (int, float)):
            raise ValueError(f"machine check task {name!r} has invalid agent_budget_s")
        verifier_budget = row.get("verifier_budget_s")
        if isinstance(verifier_budget, bool) or not isinstance(verifier_budget, (int, float)):
            raise ValueError(f"machine check task {name!r} has invalid verifier_budget_s")
        agent_budgets.append(float(agent_budget))
        verifier_budgets.append(float(verifier_budget))
    return max(agent_budgets), max(verifier_budgets)


def build_snapshot(
    session,
    job_dir: Path,
    pass_started_t: float,
    now: float,
    proxy_alive: bool,
    stop_launching_at: float | None = None,
) -> run_health.RunSnapshot:
    return _build_snapshot(
        session,
        job_dir,
        pass_started_t,
        now,
        proxy_alive,
        JsonlTail(session.run_dir / "proxy-ledger.jsonl"),
        JsonlTail(session.run_dir / "resource-stats.jsonl"),
        stop_launching_at,
    )


def _build_snapshot(
    session,
    job_dir: Path,
    pass_started_t: float,
    now: float,
    proxy_alive: bool,
    ledger_tail: JsonlTail,
    resource_tail: JsonlTail,
    stop_launching_at: float | None = None,
) -> run_health.RunSnapshot:
    ledger = ledger_tail.read()
    resources = resource_tail.read()
    rows = [
        row
        for row in ledger.rows
        if (timestamp := _timestamp(row)) is not None and timestamp >= pass_started_t
    ]
    events = _read_events(session.run_dir)
    verdicts = [row for row in events if row.get("type") == "trial_verdict"]
    judged = {row["trial"] for row in verdicts if isinstance(row.get("trial"), str)}
    opens: set[str] = set()
    closed: set[str] = set()
    for row in rows:
        trial = row.get("trial")
        if not isinstance(trial, str):
            continue
        if row.get("type") == "open":
            opens.add(trial)
        elif row.get("type") == "close":
            closed.add(trial)
    activity_times = [value for row in rows if (value := _timestamp(row)) is not None]
    for trial_dir in job_folder.trial_folders(job_dir):
        try:
            modified = (trial_dir / "result.json").stat().st_mtime
        except OSError:
            continue
        if modified >= pass_started_t:
            activity_times.append(modified)
    last_activity = max(activity_times, default=pass_started_t)
    longest_agent, longest_verifier = _task_budgets(session)
    timeout_multiplier = 1.0
    for trial_dir in job_folder.trial_folders(job_dir):
        try:
            lock = json.loads((trial_dir / "lock.json").read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        multiplier = lock.get("timeout_multiplier", 1.0) if isinstance(lock, dict) else 1.0
        if isinstance(multiplier, (int, float)) and not isinstance(multiplier, bool):
            timeout_multiplier = max(timeout_multiplier, float(multiplier))
    return run_health.RunSnapshot(
        now=now,
        ledger_rows_since_pass_start=rows,
        verdicts=verdicts,
        open_trials=len(opens - closed),
        proxy_alive=proxy_alive,
        oom_watch_alive=session.oom_watch_alive(),
        memory_watch_alive=session.memory_watch_alive(),
        resource_rows=resources.rows,
        last_activity_t=last_activity,
        longest_agent_budget_s=longest_agent,
        longest_verifier_budget_s=longest_verifier,
        harbor_timeout_multiplier=timeout_multiplier,
        stop_launching_at=stop_launching_at,
        in_flight_trials=len(opens - judged),
    )


def judged_trials(run_dir: Path) -> set[str]:
    return {
        row["trial"]
        for row in _read_events(run_dir)
        if row.get("type") == "trial_verdict" and isinstance(row.get("trial"), str)
    }


def watch(
    session,
    harbor_process,
    job_dir: Path,
    pass_started_t: float,
    poll_s: float = 5.0,
    now=time.time,
    sleep=time.sleep,
    stop_launching_at: float | None = None,
) -> WatchResult:
    if stop_launching_at is None:
        stop_launching_at = getattr(session, "stop_launching_at_epoch", None)
    judged = judged_trials(session.run_dir)
    ledger_tail = JsonlTail(session.run_dir / "proxy-ledger.jsonl")
    resource_tail = JsonlTail(session.run_dir / "resource-stats.jsonl")
    while True:
        sleep(poll_s)
        current_t = now()
        _judge_finished(session, job_dir, judged, current_t, ledger_tail, resource_tail)
        snapshot = _build_snapshot(
            session,
            job_dir,
            pass_started_t,
            current_t,
            session.proxy_alive(),
            ledger_tail,
            resource_tail,
            stop_launching_at,
        )
        halt = run_health.should_halt(snapshot, session.limits, poll_s)
        if halt is not None:
            reason, detail = halt
            process_group.stop_group(harbor_process, grace_s=process_group.HARBOR_STOP_GRACE_S)
            session.event(Halt(current_t, reason, detail))
            return WatchResult("halted", None, reason, detail)
        returncode = harbor_process.poll()
        if returncode is not None:
            return WatchResult("exited", returncode, None, None)


def _timestamp(row: dict) -> float | None:
    value = row.get("t")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)
