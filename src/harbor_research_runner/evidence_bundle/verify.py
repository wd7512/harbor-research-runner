from __future__ import annotations

import gzip
import json
import re
from dataclasses import dataclass
from pathlib import Path

from harbor_research_runner.records.jsonl import ReadResult, bad_line_time_spans, read_rows
from harbor_research_runner.records.manifest import verify_manifest
from harbor_research_runner.records.run_events import final_verdicts, telemetry_finished
from harbor_research_runner.records.trials import CAP_EXEMPT_REASONS


@dataclass(frozen=True)
class Problem:
    invariant: str
    subject: str
    expected: object
    actual: object

    def __str__(self) -> str:
        return f"{self.invariant} [{self.subject}]: expected {self.expected!r}, got {self.actual!r}"


def _json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} must contain a JSON object")
    return value


def _jsonl_gzip(path: Path) -> list[dict]:
    raw = gzip.decompress(path.read_bytes())
    rows = []
    for number, line in enumerate(raw.splitlines(), start=1):
        value = json.loads(line.decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError(f"{path.name}:{number} must contain a JSON object")
        rows.append(value)
    return rows


def _problem(
    problems: list[Problem], invariant: str, subject: str, expected: object, actual: object
) -> None:
    problems.append(Problem(invariant, subject, expected, actual))


def _report_counts(report: str) -> dict[str, int]:
    counts = {}
    for label in (
        "Slots planned",
        "Slots complete",
        "Slots incomplete",
        "Unmeasured trials",
        "Trials not yet judged",
        "Malformed resource samples",
        "Malformed Docker OOM events",
        "Malformed memory-watch samples",
    ):
        matches = re.findall(rf"^\| {re.escape(label)} \| (\d+) \|$", report, re.MULTILINE)
        if len(matches) == 1:
            counts[label] = int(matches[0])
    return counts


def malformed_jsonl_count(data: bytes) -> int:
    if not data:
        return 0
    lines = data.split(b"\n")
    if lines[-1] == b"":
        lines.pop()
    count = 0
    for line in lines:
        try:
            value = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            count += 1
        else:
            if not isinstance(value, dict):
                count += 1
    return count


def _resource_rows(data: bytes) -> tuple[list[tuple[int, dict]], list[int], bool]:
    rows = []
    bad_lines = []
    lines = data.split(b"\n")
    ends_with_newline = data.endswith(b"\n")
    if ends_with_newline:
        lines.pop()
    torn_last_line = False
    for number, line in enumerate(lines, start=1):
        is_last = number == len(lines)
        has_newline = number < len(lines) or ends_with_newline
        try:
            row = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            if is_last:
                torn_last_line = True
            else:
                bad_lines.append(number)
        else:
            if isinstance(row, dict):
                rows.append((number, row))
                if is_last and not has_newline:
                    torn_last_line = True
            else:
                if is_last:
                    torn_last_line = True
                else:
                    bad_lines.append(number)
    return rows, bad_lines, torn_last_line


def _host_window_damaged(
    row: dict,
    resource_rows: list[tuple[int, dict]],
    bad_lines: list[int],
    torn_last_line: bool,
    interval_s: float,
    *,
    finished: bool,
) -> bool:
    start = row.get("agent_started_t")
    end = (
        row.get("verifier_finished_t")
        if row.get("verifier_started_t") is not None
        else row.get("agent_finished_t")
    )
    if not isinstance(start, (int, float)) or isinstance(start, bool):
        return False
    if not isinstance(end, (int, float)) or isinstance(end, bool):
        return False
    samples = sorted(
        (
            (line, sample)
            for line, sample in resource_rows
            if sample.get("type") == "sample"
            and isinstance(sample.get("t"), (int, float))
            and not isinstance(sample.get("t"), bool)
        ),
        key=lambda item: item[1]["t"],
    )
    times = [float(sample["t"]) for _line, sample in samples]
    if not times:
        return True
    threshold = 3 * interval_s
    if any(
        current - previous > threshold and previous <= end and current >= start
        for previous, current in zip(times, times[1:], strict=False)
    ):
        return True
    if any(min(abs(edge - timestamp) for timestamp in times) > threshold for edge in (start, end)):
        return True
    sample_result = ReadResult(
        [sample for _line, sample in resource_rows], torn_last_line, bad_lines
    )
    for span_start, span_end in bad_line_time_spans(sample_result, finished=finished):
        if span_start <= end and span_end >= start:
            return True
    return False


def _trial_telemetry_damaged(
    row: dict, oom: ReadResult, memory: ReadResult, *, finished: bool
) -> bool:
    start = row.get("agent_started_t")
    end = (
        row.get("verifier_finished_t")
        if row.get("verifier_started_t") is not None
        else row.get("agent_finished_t")
    )
    if (
        not isinstance(start, (int, float))
        or isinstance(start, bool)
        or not isinstance(end, (int, float))
        or isinstance(end, bool)
    ):
        return False
    spans = bad_line_time_spans(oom, "timeNano", 1e-9, finished=finished)
    spans.extend(bad_line_time_spans(memory, finished=finished))
    return any(span_start <= end and span_end >= start for span_start, span_end in spans)


def verify(bundle_dir: Path) -> list[Problem]:
    problems = [
        Problem("manifest", text.split(":", 1)[0], "valid manifest", text)
        for text in verify_manifest(bundle_dir)
    ]
    trial_rows_result = read_rows(bundle_dir / "trials.jsonl")
    event_rows_result = read_rows(bundle_dir / "run-events.jsonl")
    trials = trial_rows_result.rows
    events = event_rows_result.rows
    finished = telemetry_finished(events)
    memory_path = bundle_dir / "memory-watch.jsonl"
    memory_rows_result = read_rows(memory_path) if memory_path.exists() else None
    memory_rows = memory_rows_result.rows if memory_rows_result is not None else []
    oom_path = bundle_dir / "docker-oom-events.jsonl"
    oom_result = read_rows(oom_path) if oom_path.exists() else None
    oom_rows_result = oom_result if oom_result is not None else ReadResult([], False, [])
    memory_rows_result = (
        memory_rows_result if memory_rows_result is not None else ReadResult([], False, [])
    )
    request_rows = _jsonl_gzip(bundle_dir / "proxy-requests.jsonl.gz")
    metadata = _json(bundle_dir / "run-metadata.json")
    report = (bundle_dir / "report.md").read_text(encoding="utf-8")
    resource_path = bundle_dir / "resource-stats.jsonl.gz"
    resource_data = gzip.decompress(resource_path.read_bytes()) if resource_path.exists() else None
    resource_rows, resource_bad_lines, resource_torn_last_line = (
        _resource_rows(resource_data) if resource_data is not None else ([], [], False)
    )
    sampler_interval_s = metadata.get("sampler_interval_s", 10.0)
    if (
        isinstance(sampler_interval_s, bool)
        or not isinstance(sampler_interval_s, (int, float))
        or sampler_interval_s <= 0
    ):
        _problem(
            problems,
            "sampler_interval",
            "run-metadata.json",
            "positive number",
            sampler_interval_s,
        )
        sampler_interval_s = 10.0

    if any(event.get("type") == "harbor_start" for event in events):
        for name in (
            "resource-stats.jsonl.gz",
            "docker-oom-events.jsonl",
            "memory-watch.jsonl",
        ):
            if not (bundle_dir / name).exists():
                _problem(problems, "telemetry_present", name, "present", "missing")

    by_trial: dict[str, list[dict]] = {}
    for row in trials:
        by_trial.setdefault(row["trial"], []).append(row)
    verdict_events: dict[str, list[dict]] = {}
    for event in events:
        if event.get("type") == "trial_verdict":
            verdict_events.setdefault(event["trial"], []).append(event)
    verdicts = final_verdicts(events)
    amendments_without_verdict = [
        event.get("trial")
        for event in events
        if event.get("type") == "trial_measurement_amended"
        and (not isinstance(event.get("trial"), str) or event.get("trial") not in verdict_events)
    ]
    for trial in sorted(amendments_without_verdict, key=str):
        _problem(
            problems,
            "trial_identity",
            str(trial),
            "trial_verdict event",
            "amendment without verdict",
        )
    for trial, rows in by_trial.items():
        if len(rows) != 1:
            _problem(problems, "trial_identity", trial, 1, len(rows))
        matching = verdict_events.get(trial, [])
        if len(matching) != 1:
            _problem(problems, "trial_identity", trial, 1, len(matching))
        elif len(rows) == 1:
            row, verdict = rows[0], verdicts[trial]
            actual = {
                key: row.get(key) for key in ("valid", "measured", "reason", "unmeasured_reason")
            }
            expected = {
                key: verdict.get(key)
                for key in ("valid", "measured", "reason", "unmeasured_reason")
            }
            if actual != expected:
                _problem(problems, "trial_identity", trial, expected, actual)
    for trial in verdict_events:
        if trial not in by_trial:
            _problem(problems, "trial_identity", trial, "trial row", "verdict without trial row")

    for row in trials:
        trial = row["trial"]
        telemetry_damaged = (
            oom_path.exists() or memory_path.exists()
        ) and _trial_telemetry_damaged(row, oom_rows_result, memory_rows_result, finished=finished)
        verdict = verdicts.get(trial)
        host_damaged = (
            resource_data is not None
            and any(event.get("type") == "harbor_start" for event in events)
            and _host_window_damaged(
                row,
                resource_rows,
                resource_bad_lines,
                resource_torn_last_line,
                float(sampler_interval_s),
                finished=finished,
            )
        )
        expected_damage = (
            "telemetry_damaged"
            if telemetry_damaged
            else "host_samples_damaged"
            if host_damaged
            else None
        )
        recorded_damage = (
            verdict.get("unmeasured_reason")
            if verdict is not None
            and verdict.get("measured") is False
            and verdict.get("unmeasured_reason") in {"telemetry_damaged", "host_samples_damaged"}
            else None
        )
        if (
            verdict is not None
            and (verdict.get("measured") is True or recorded_damage is not None)
            and recorded_damage != expected_damage
        ):
            _problem(
                problems,
                expected_damage or recorded_damage or "telemetry_damaged",
                trial,
                (
                    {"measured": False, "unmeasured_reason": expected_damage}
                    if expected_damage is not None
                    else {"measured": True, "unmeasured_reason": None}
                ),
                {
                    "measured": verdict.get("measured"),
                    "unmeasured_reason": verdict.get("unmeasured_reason"),
                },
            )
        if row.get("location") == "set-aside-trials" and row.get("valid") is not False:
            _problem(problems, "locations", trial, False, row.get("valid"))

    task_set = metadata.get("task_set", {}).get("tasks", [])
    arms = metadata.get("arms", [])
    task_names = {task["name"] for task in task_set}
    arm_names = {arm["label"] for arm in arms}
    if not task_set:
        _problem(problems, "task_set", "tasks", "non-empty task set", task_set)
    for row in trials:
        if row.get("arm") not in arm_names:
            _problem(problems, "task_set", row["trial"], sorted(arm_names), row.get("arm"))
        if row.get("location") == "canary-job":
            continue
        if row.get("task") not in task_names:
            _problem(problems, "task_set", row["trial"], sorted(task_names), row.get("task"))

    repetitions = metadata["job"]["repetitions"]
    planned_slots = {(task["name"], arm["label"]) for task in task_set for arm in arms}
    final_counts: dict[tuple[str, str], int] = {}
    for row in trials:
        if row.get("location") == "harbor-job" and row.get("valid") is True:
            slot = (row["task"], row["arm"])
            final_counts[slot] = final_counts.get(slot, 0) + 1
    slots_complete = sum(final_counts.get(slot, 0) >= repetitions for slot in planned_slots)
    slots_incomplete = len(planned_slots) - slots_complete
    non_canary = [row for row in trials if row.get("location") != "canary-job"]
    unmeasured = sum(row.get("measured") is False for row in non_canary)
    unjudged_trials = {
        request["trial"] for request in request_rows if request.get("trial") not in by_trial
    }
    expected_counts = {
        "Slots planned": len(planned_slots),
        "Slots complete": slots_complete,
        "Slots incomplete": slots_incomplete,
        "Unmeasured trials": unmeasured,
        "Malformed resource samples": (
            malformed_jsonl_count(resource_data) if resource_data is not None else 0
        ),
    }
    if oom_path.exists():
        expected_counts["Malformed Docker OOM events"] = malformed_jsonl_count(
            oom_path.read_bytes()
        )
    if memory_path.exists():
        expected_counts["Malformed memory-watch samples"] = malformed_jsonl_count(
            memory_path.read_bytes()
        )
    actual_counts = _report_counts(report)
    has_finish = any(event.get("type") == "finish" for event in events)
    expected_counts["Trials not yet judged"] = (
        0 if has_finish else actual_counts.get("Trials not yet judged")
    )
    if actual_counts != expected_counts:
        _problem(problems, "report_agreement", "report.md", expected_counts, actual_counts)
    if (
        actual_counts.get("Malformed resource samples")
        != expected_counts["Malformed resource samples"]
    ):
        _problem(
            problems,
            "resource_samples",
            "resource-stats.jsonl.gz",
            expected_counts["Malformed resource samples"],
            actual_counts.get("Malformed resource samples"),
        )

    for event in events:
        if event.get("type") != "finish":
            continue
        expected = {"slots_complete": slots_complete, "slots_incomplete": slots_incomplete}
        actual = {key: event.get(key) for key in expected}
        if actual != expected:
            _problem(problems, "finish_agreement", "finish", expected, actual)
        if event.get("exit_code") == 0 and slots_incomplete != 0:
            _problem(problems, "finish_agreement", "finish.exit_code", 0, slots_incomplete)

    limits = metadata.get("limits", {})
    cap = limits.get("max_set_asides_per_slot", 2 * repetitions)
    set_aside_counts: dict[tuple[str, str], int] = {}
    for event in events:
        if event.get("type") != "set_aside" or event.get("reason") in CAP_EXEMPT_REASONS:
            continue
        slot = (event["task"], event["arm"])
        set_aside_counts[slot] = set_aside_counts.get(slot, 0) + 1
    for slot, count in set_aside_counts.items():
        if count > cap:
            _problem(problems, "set_aside_cap", "/".join(slot), cap, count)

    samples: dict[str, list[float]] = {}
    for sample in memory_rows:
        if sample.get("type") != "sample" or sample.get("location") not in {
            "harbor-job",
            "canary-job",
        }:
            continue
        samples.setdefault(sample["trial"], []).append(sample["used_mb"])
    for row in trials:
        values = samples.get(row["trial"], [])
        damaged = _trial_telemetry_damaged(
            row, oom_rows_result, memory_rows_result, finished=finished
        )
        if values or damaged:
            expected = None if damaged else max(values)
            actual = row.get("peak_memory_mb")
            if actual != expected:
                _problem(problems, "memory", row["trial"], expected, actual)

    requests_by_trial: dict[str, list[dict]] = {}
    for request in request_rows:
        trial = request["trial"]
        requests_by_trial.setdefault(trial, []).append(request)
    if has_finish:
        for trial in sorted(unjudged_trials):
            _problem(
                problems,
                "proxy_accounting",
                trial,
                "trial row or documented non-trial id",
                "request without trial row",
            )
    for trial, rows in by_trial.items():
        if len(rows) != 1:
            continue
        row = rows[0]
        requests = requests_by_trial.get(trial, [])
        completed = [request for request in requests if request.get("upstream_status") is not None]
        expected = {
            "requests": len(completed),
            "prompt_tokens": sum(request.get("prompt_tokens") or 0 for request in completed),
            "completion_tokens": sum(
                request.get("completion_tokens") or 0 for request in completed
            ),
            "cached_read_tokens": sum(
                request.get("cached_read_tokens") or 0 for request in completed
            ),
        }
        actual = {key: row.get(key) for key in expected}
        if actual != expected:
            _problem(problems, "proxy_accounting", trial, expected, actual)
        unfinished = [request for request in requests if request.get("upstream_status") is None]
        if row.get("measured") is True and unfinished:
            _problem(problems, "measured_closed", trial, "no unfinished requests", len(unfinished))

    if trial_rows_result.bad_lines or event_rows_result.bad_lines:
        raise ValueError("bundle contains malformed JSONL records")
    return problems
