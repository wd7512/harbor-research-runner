from dataclasses import dataclass

from harbor_research_runner.records.jsonl import ReadResult
from harbor_research_runner.records.limits import Limits, host_breach
from harbor_research_runner.records.proxy_ledger import parse_row, rows_for_trial
from harbor_research_runner.trial_validity.phase_table import failed_phase
from harbor_research_runner.trial_validity.upstream import upstream_reason


def compose_project(trial: str) -> str:
    return f"{trial.lower()}__env"


@dataclass(frozen=True)
class TrialFacts:
    trial: str
    task: str
    arm: str
    has_result: bool
    reward: float | None
    exception_type: str | None
    exception_at: float | None
    phase_started: dict[str, float | None]
    phase_finished: dict[str, float | None]
    harbor_reported_tokens: int


@dataclass(frozen=True)
class Verdict:
    valid: bool
    reason: str | None
    instrument: str | None
    labels: tuple[str, ...]
    measured: bool
    unmeasured_reason: str | None
    oom_source: str | None = None


def _valid(row: dict) -> bool:
    try:
        parse_row(row)
    except ValueError:
        return False
    return True


def _physical_positions(problems: ReadResult) -> list[int]:
    total = len(problems.rows) + len(problems.bad_lines) + (1 if problems.torn_last_line else 0)
    bad_lines = set(problems.bad_lines)
    positions = []
    for line in range(1, total + 1):
        if line in bad_lines:
            continue
        if problems.torn_last_line and line == total:
            continue
        positions.append(line)
    return positions


def _view(problems: ReadResult) -> list[tuple[dict, int, bool]]:
    positions = _physical_positions(problems)
    return [
        (row, position, _valid(row))
        for row, position in zip(problems.rows, positions, strict=False)
    ]


def _boundary(
    view: list[tuple[dict, int, bool]], trial: str
) -> tuple[tuple[dict, int] | None, tuple[dict, int] | None]:
    open_entry = None
    close_entry = None
    for row, position, _ in view:
        if row.get("trial") != trial:
            continue
        if open_entry is None and row.get("type") == "open":
            open_entry = (row, position)
        if close_entry is None and row.get("type") == "close":
            close_entry = (row, position)
    return open_entry, close_entry


def _positive_seq(row: dict) -> int | None:
    seq = row.get("seq")
    return seq if type(seq) is int and seq > 0 else None


def _position_within(open_position: int, close_position: int | None, position: int) -> bool:
    if close_position is None:
        return position > open_position
    return open_position < position < close_position


def _gap_within(open_seq: int, close_seq: int | None, previous: int, current: int) -> bool:
    if close_seq is None:
        return max(previous, open_seq) < current
    return max(previous, open_seq) < min(current, close_seq)


def _reset_within(open_position: int, close_position: int | None, position: int) -> bool:
    if close_position is None:
        return position >= open_position
    return open_position <= position <= close_position


def _measurement(
    trial: str, trial_rows: list[dict], problems: ReadResult
) -> tuple[bool, str | None]:
    view = _view(problems)
    open_entry, close_entry = _boundary(view, trial)
    open_position = open_entry[1] if open_entry else None
    close_position = close_entry[1] if close_entry else None
    open_seq = _positive_seq(open_entry[0]) if open_entry else None
    close_seq = _positive_seq(close_entry[0]) if close_entry else None

    valid_rows = []
    damaged = False
    for row in trial_rows:
        if _valid(row):
            valid_rows.append(row)
        else:
            damaged = True

    if not damaged and open_position is not None:
        for _, position, valid in view:
            if not valid and _position_within(open_position, close_position, position):
                damaged = True
                break

    if not damaged and open_position is not None:
        for bad_line in problems.bad_lines:
            if _position_within(open_position, close_position, bad_line):
                damaged = True
                break

    if not damaged:
        seq_entries = []
        for row, position, _ in view:
            seq = _positive_seq(row)
            if seq is not None:
                seq_entries.append((seq, position))
        for (previous, _), (current, current_position) in zip(
            seq_entries, seq_entries[1:], strict=False
        ):
            if current > previous + 1:
                if open_seq is not None and _gap_within(open_seq, close_seq, previous, current):
                    damaged = True
                    break
            elif current <= previous and (
                open_position is not None
                and _reset_within(open_position, close_position, current_position)
            ):
                damaged = True
                break

    if damaged:
        return False, "ledger_damaged"

    starts = [row for row in valid_rows if row.get("type") == "request_start"]
    ends = [row for row in valid_rows if row.get("type") == "request_end"]
    has_close = any(row.get("type") == "close" for row in valid_rows)
    started_ids = {row["request_id"] for row in starts}
    ended_ids = {row["request_id"] for row in ends}
    if has_close and started_ids - ended_ids:
        return False, "request_not_recorded"

    has_open = any(row.get("type") == "open" for row in valid_rows)
    if not has_open and (starts or ends):
        return False, "no_open_row"
    return True, None


def _host_fault(facts: TrialFacts, resource_rows: list[dict], limits: Limits) -> bool:
    start = facts.phase_started.get("agent")
    if start is None:
        return False
    if facts.phase_started.get("verifier") is not None:
        end = facts.phase_finished.get("verifier")
    else:
        end = facts.phase_finished.get("agent")
    if end is None:
        return False
    for sample in resource_rows:
        t = sample.get("t")
        if not isinstance(t, (int, float)) or isinstance(t, bool):
            continue
        if start <= t <= end and host_breach(sample, limits):
            return True
    return False


def _labels(
    facts: TrialFacts,
    phase: str | None,
    trial_rows: list[dict],
    setup_repeated: bool,
    oom_killed: bool,
) -> tuple[str, ...]:
    labels = []
    if setup_repeated:
        labels.append("harness_could_not_start")
    if oom_killed:
        labels.append("oom_killed")
    elif facts.exception_type == "AgentTimeoutError":
        labels.append("timed_out")
    elif phase == "agent" and facts.exception_type is not None:
        labels.append("exit_nonzero")
    if phase == "verifier":
        labels.append("verifier_failed")
    if not any(row.get("type") == "request_start" for row in trial_rows):
        labels.append("no_requests")
    if facts.harbor_reported_tokens > 0 and not any(
        row.get("type") == "request_end" for row in trial_rows
    ):
        labels.append("possible_bypass")
    if any(row.get("type") == "refused" and row.get("reason") == "closed" for row in trial_rows):
        labels.append("refused_after_close")
    return tuple(labels)


def judge_trial(
    facts: TrialFacts,
    ledger_rows: list[dict],
    ledger_problems: ReadResult,
    resource_rows: list[dict],
    limits: Limits,
    earlier_set_aside_reasons: tuple[str, ...],
    oom_events: list[dict] | tuple[()] = (),
    memory_watch_events: list[dict] | tuple[()] = (),
) -> Verdict:
    """Decide one trial's validity, applying rules 1 to 11 in order."""
    phase = failed_phase(facts)
    trial_rows = rows_for_trial(ledger_rows, facts.trial)
    usable_rows = [row for row in trial_rows if _valid(row)]
    measured, unmeasured_reason = _measurement(facts.trial, trial_rows, ledger_problems)
    start = facts.phase_started.get("agent")
    end = (
        facts.phase_finished.get("verifier")
        if facts.phase_started.get("verifier") is not None
        else facts.phase_finished.get("agent")
    )
    oom_project = compose_project(facts.trial)
    oom_sources = []
    if start is not None and end is not None:
        for event in oom_events:
            if not isinstance(event, dict):
                continue
            event_time = event.get("timeNano")
            project = event.get("Actor", {}).get("Attributes", {}).get("com.docker.compose.project")
            if (
                project == oom_project
                and isinstance(event_time, (int, float))
                and not isinstance(event_time, bool)
            ):
                timestamp = event_time / 1_000_000_000
                if start <= timestamp <= end:
                    oom_sources.append((timestamp, "kernel"))
        for event in memory_watch_events:
            if (
                isinstance(event, dict)
                and event.get("type") == "kill"
                and event.get("location") == "harbor-job"
                and event.get("trial") == facts.trial
                and isinstance(event.get("t"), (int, float))
                and not isinstance(event.get("t"), bool)
                and start <= event["t"] <= end
            ):
                oom_sources.append((float(event["t"]), "watchdog"))
    oom_source = min(oom_sources)[1] if oom_sources else None
    oom_killed = oom_source is not None

    def invalid(reason: str, instrument: str) -> Verdict:
        return Verdict(False, reason, instrument, (), measured, unmeasured_reason, None)

    if not facts.has_result:
        return invalid("never_finished", "harbor")
    if facts.exception_type == "CancelledError":
        return invalid("cancelled", "harbor")
    if phase == "environment_setup":
        return invalid("environment_failed", "harbor")
    if phase == "agent_setup":
        if "harness_setup_failed" not in earlier_set_aside_reasons:
            return invalid("harness_setup_failed", "harbor")
        return Verdict(
            True,
            None,
            None,
            _labels(facts, phase, trial_rows, True, False),
            measured,
            unmeasured_reason,
            None,
        )
    if _host_fault(facts, resource_rows, limits):
        return invalid("host_fault", "host")
    reason = upstream_reason(usable_rows, limits)
    if reason is not None:
        return invalid(reason, "proxy")
    if facts.reward is None and phase != "verifier" and not oom_killed:
        return invalid("no_verdict", "harbor")

    return Verdict(
        True,
        None,
        None,
        _labels(facts, phase, trial_rows, False, oom_killed),
        measured,
        unmeasured_reason,
        oom_source,
    )
