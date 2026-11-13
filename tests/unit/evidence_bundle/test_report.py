import json
from itertools import product
from pathlib import Path

from harbor_research_runner.evidence_bundle.report import render_report
from harbor_research_runner.evidence_bundle.trials import build_trial_rows
from harbor_research_runner.records.machine_check import read

FIXTURES = Path(__file__).parents[2] / "fixtures" / "run_folders"


def _render(folder: str) -> str:
    run_dir = FIXTURES / folder
    rows = build_trial_rows(run_dir)
    metadata = json.loads((run_dir / "run-metadata.json").read_text(encoding="utf-8"))
    events = [
        json.loads(line)
        for line in (run_dir / "run-events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    tasks = sorted({row.task for row in rows if row.location != "canary-job"})
    arms = [arm["label"] for arm in metadata["arms"]]
    planned_slots = list(product(tasks, arms))
    return render_report(
        rows,
        metadata,
        events,
        planned_slots,
        metadata["job"]["repetitions"],
        machine_check=read(run_dir / "machine-check.json"),
    )


def test_clean_run_report_matches_golden_and_repeats_byte_identically():
    expected = (FIXTURES / "clean" / "expected-report.md").read_text(encoding="utf-8")

    first = _render("clean")
    assert first == expected
    assert _render("clean") == first
    assert "## Set-aside trials" in first
    assert "| None | 0 |" in first
    assert "Tokens spent: 0" in first


def test_retried_run_report_matches_golden():
    expected = (FIXTURES / "retries" / "expected-report.md").read_text(encoding="utf-8")

    assert _render("retries") == expected


def test_report_lists_machine_check_exclusions():
    report = _render("clean")

    assert "2 excluded by the machine check (task/d, task/e)" in report


def test_report_includes_host_memory_cap_and_rootless_status():
    run_dir = FIXTURES / "clean"
    rows = build_trial_rows(run_dir)
    metadata = json.loads((run_dir / "run-metadata.json").read_text(encoding="utf-8"))
    metadata["host"]["host_profile"] = {"memory_cap": "kernel", "rootless": False}
    events = [
        json.loads(line)
        for line in (run_dir / "run-events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    tasks = sorted({row.task for row in rows if row.location != "canary-job"})
    report = render_report(
        rows,
        metadata,
        events,
        list(product(tasks, [arm["label"] for arm in metadata["arms"]])),
        metadata["job"]["repetitions"],
        machine_check=read(run_dir / "machine-check.json"),
    )

    assert "Machine: fixture-host, memory_cap=kernel, rootless=false" in report
