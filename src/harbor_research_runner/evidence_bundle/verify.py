from __future__ import annotations

import gzip
import json
import re
from dataclasses import dataclass
from pathlib import Path

from harbor_research_runner.records.jsonl import read_rows
from harbor_research_runner.records.manifest import verify_manifest


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
    ):
        matches = re.findall(rf"^\| {re.escape(label)} \| (\d+) \|$", report, re.MULTILINE)
        if len(matches) == 1:
            counts[label] = int(matches[0])
    return counts


def verify(bundle_dir: Path) -> list[Problem]:
    problems = [
        Problem("manifest", text.split(":", 1)[0], "valid manifest", text)
        for text in verify_manifest(bundle_dir)
    ]
    trial_rows_result = read_rows(bundle_dir / "trials.jsonl")
    event_rows_result = read_rows(bundle_dir / "run-events.jsonl")
    memory_rows_result = read_rows(bundle_dir / "memory-watch.jsonl")
    trials = trial_rows_result.rows
    events = event_rows_result.rows
    memory_rows = memory_rows_result.rows
    request_rows = _jsonl_gzip(bundle_dir / "proxy-requests.jsonl.gz")
    metadata = _json(bundle_dir / "run-metadata.json")
    report = (bundle_dir / "report.md").read_text(encoding="utf-8")

    by_trial: dict[str, list[dict]] = {}
    for row in trials:
        by_trial.setdefault(row["trial"], []).append(row)
    verdicts: dict[str, list[dict]] = {}
    for event in events:
        if event.get("type") == "trial_verdict":
            verdicts.setdefault(event["trial"], []).append(event)
    for trial, rows in by_trial.items():
        if len(rows) != 1:
            _problem(problems, "trial_identity", trial, 1, len(rows))
        matching = verdicts.get(trial, [])
        if len(matching) != 1:
            _problem(problems, "trial_identity", trial, 1, len(matching))
        elif len(rows) == 1:
            row, verdict = rows[0], matching[0]
            actual = {
                key: row.get(key) for key in ("valid", "measured", "reason", "unmeasured_reason")
            }
            expected = {
                key: verdict.get(key)
                for key in ("valid", "measured", "reason", "unmeasured_reason")
            }
            if actual != expected:
                _problem(problems, "trial_identity", trial, expected, actual)
    for trial in verdicts:
        if trial not in by_trial:
            _problem(problems, "trial_identity", trial, "trial row", "verdict without trial row")

    for row in trials:
        trial = row["trial"]
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
        if (
            row.get("location") == "harbor-job"
            and row.get("valid") is True
            and row.get("measured") is True
        ):
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
    }
    actual_counts = _report_counts(report)
    has_finish = any(event.get("type") == "finish" for event in events)
    if "Trials not yet judged" in actual_counts:
        expected_counts["Trials not yet judged"] = (
            0 if has_finish else actual_counts["Trials not yet judged"]
        )
    if actual_counts != expected_counts:
        _problem(problems, "report_agreement", "report.md", expected_counts, actual_counts)

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
        if event.get("type") != "set_aside" or event.get("reason") == "deferred":
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
        if values:
            expected = max(values)
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

    if trial_rows_result.bad_lines or event_rows_result.bad_lines or memory_rows_result.bad_lines:
        raise ValueError("bundle contains malformed JSONL records")
    return problems
