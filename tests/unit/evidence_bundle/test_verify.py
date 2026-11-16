from __future__ import annotations

import gzip
import hashlib
import json
import shutil
from pathlib import Path

import pytest

from harbor_research_runner.evidence_bundle.build import build
from harbor_research_runner.evidence_bundle.trials import build_trial_rows
from harbor_research_runner.evidence_bundle.verify import Problem, verify
from harbor_research_runner.records.manifest import write_manifest

FIXTURES = Path(__file__).parents[2] / "fixtures" / "run_folders"


def _bundle(source: str, tmp_path: Path) -> Path:
    run_dir = tmp_path / f"run-{source}"
    shutil.copytree(FIXTURES / source, run_dir)
    metadata_path = run_dir / "run-metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["machine_check_sha256"] = hashlib.sha256(
        (run_dir / "machine-check.json").read_bytes()
    ).hexdigest()
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    bundle_dir = tmp_path / f"bundle-{source}"
    assert build(run_dir, bundle_dir, {}) in {0, 5}
    return bundle_dir


def _rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _write_rows(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows))


def _refresh_manifest(bundle: Path) -> None:
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    write_manifest(bundle, manifest["tool_version"], manifest["tool_commit"], manifest["run_id"])


def _finished_telemetry_bundle(
    tmp_path: Path,
    *,
    amendment: bool,
    stopped: bool = True,
    unknown_amendment: bool = False,
) -> Path:
    run_dir = tmp_path / "finished-run"
    shutil.copytree(FIXTURES / "clean", run_dir)
    metadata_path = run_dir / "run-metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["machine_check_sha256"] = hashlib.sha256(
        (run_dir / "machine-check.json").read_bytes()
    ).hexdigest()
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    latest = max(
        (row for row in build_trial_rows(run_dir) if row.verifier_finished_t is not None),
        key=lambda row: row.verifier_finished_t,
    )
    sample = {
        "type": "sample",
        "t": latest.verifier_finished_t - 0.5,
        "trial": latest.trial,
        "location": latest.location,
        "container": "container",
        "used_mb": 100.0,
        "limit_mb": 512,
        "reader": "docker_stats",
    }
    (run_dir / "memory-watch.jsonl").write_text(json.dumps(sample) + "\nnot-json", encoding="utf-8")
    events_path = run_dir / "run-events.jsonl"
    events = _rows(events_path)
    if amendment:
        events.append(
            {
                "type": "trial_measurement_amended",
                "t": latest.verifier_finished_t + 1,
                "trial": latest.trial,
                "unmeasured_reason": "telemetry_damaged",
            }
        )
    if unknown_amendment:
        events.append(
            {
                "type": "trial_measurement_amended",
                "t": latest.verifier_finished_t + 1,
                "trial": "unknown-trial",
                "unmeasured_reason": "telemetry_damaged",
            }
        )
    if stopped:
        events.append({"type": "telemetry_stopped", "t": latest.verifier_finished_t + 2})
    _write_rows(events_path, events)
    bundle = tmp_path / "finished-bundle"
    build(run_dir, bundle, {})
    return bundle


def _host_sample_bundle(
    tmp_path: Path,
    samples: list[dict | bytes],
    *,
    start: object = 100,
    end: object = 110,
    verifier_started: object = 101,
    verifier_finished: object = 110,
) -> Path:
    bundle = _bundle("clean", tmp_path)
    trials_path = bundle / "trials.jsonl"
    trials = _rows(trials_path)
    trial = next(row for row in trials if row["location"] == "harbor-job")
    for row in trials:
        if row is trial:
            row.update(
                agent_started_t=start,
                agent_finished_t=end,
                verifier_started_t=verifier_started,
                verifier_finished_t=verifier_finished,
            )
        else:
            row.update(
                agent_started_t=None,
                agent_finished_t=None,
                verifier_started_t=None,
                verifier_finished_t=None,
            )
    _write_rows(trials_path, trials)
    events_path = bundle / "run-events.jsonl"
    events = _rows(events_path)
    events.append({"type": "harbor_start", "t": 1, "pass": 0})
    _write_rows(events_path, events)
    (bundle / "docker-oom-events.jsonl").write_text("", encoding="utf-8")
    (bundle / "memory-watch.jsonl").write_text("", encoding="utf-8")
    report_path = bundle / "report.md"
    report = report_path.read_text(encoding="utf-8")
    report_path.write_text(
        report.replace(
            "| Tokens spent on set-aside trials |",
            "| Malformed Docker OOM events | 0 |\n"
            "| Malformed memory-watch samples | 0 |\n"
            "| Tokens spent on set-aside trials |",
        ),
        encoding="utf-8",
    )
    resource_lines = []
    malformed_lines = 0
    for sample in samples:
        if isinstance(sample, bytes):
            resource_lines.append(sample)
            malformed_lines += 1
        else:
            resource_lines.append(
                json.dumps(
                    {
                        "type": "sample",
                        "t": 100,
                        "mem_available_mb": 1024,
                        "swapped_out_pages": 0,
                        "disk_free_gb": 10.0,
                        "load1": 0.1,
                        "trial_containers": 1,
                        "docker_ok": True,
                        **sample,
                    },
                    sort_keys=True,
                ).encode("utf-8")
            )
    resource_data = b"\n".join(resource_lines) + (b"\n" if resource_lines else b"")
    (bundle / "resource-stats.jsonl.gz").write_bytes(gzip.compress(resource_data, mtime=0))
    if malformed_lines:
        report_path = bundle / "report.md"
        report = report_path.read_text(encoding="utf-8")
        report_path.write_text(
            report.replace(
                "| Malformed resource samples | 0 |",
                f"| Malformed resource samples | {malformed_lines} |",
            ),
            encoding="utf-8",
        )
    _refresh_manifest(bundle)
    return bundle


def _problem_ids(problems) -> list[str]:
    return [problem.invariant for problem in problems]


def _assert_host_sample_result(bundle: Path, damaged: bool) -> None:
    trials = _rows(bundle / "trials.jsonl")
    trial = next(row["trial"] for row in trials if row["location"] == "harbor-job")
    expected = (
        [
            Problem(
                "host_samples_damaged",
                trial,
                {"measured": False, "unmeasured_reason": "host_samples_damaged"},
                {"measured": True, "unmeasured_reason": None},
            )
        ]
        if damaged
        else []
    )

    assert verify(bundle) == expected


def test_clean_and_retry_bundles_verify(tmp_path: Path) -> None:
    assert verify(_bundle("clean", tmp_path)) == []
    assert verify(_bundle("retries", tmp_path)) == []


def test_unmeasured_reason_mismatch_is_reported(tmp_path: Path) -> None:
    bundle = _bundle("retries", tmp_path)
    trials_path = bundle / "trials.jsonl"
    trials = _rows(trials_path)
    trial = next(row for row in trials if row["trial"] == "unmeasured")
    assert trial["measured"] is False
    assert trial["unmeasured_reason"] == "ledger_damaged"
    trial["unmeasured_reason"] = "other_reason"
    _write_rows(trials_path, trials)
    _refresh_manifest(bundle)

    assert verify(bundle) == [
        Problem(
            "trial_identity",
            "unmeasured",
            {
                "valid": True,
                "measured": False,
                "reason": None,
                "unmeasured_reason": "ledger_damaged",
            },
            {
                "valid": True,
                "measured": False,
                "reason": None,
                "unmeasured_reason": "other_reason",
            },
        )
    ]


def test_unmeasured_reason_matches_the_recorded_verdict(tmp_path: Path) -> None:
    bundle = _bundle("retries", tmp_path)
    trials = _rows(bundle / "trials.jsonl")
    trial = next(row for row in trials if row["trial"] == "unmeasured")

    assert trial["measured"] is False
    assert trial["unmeasured_reason"] == "ledger_damaged"
    assert verify(bundle) == []


def test_duplicate_trial_does_not_hide_later_proxy_accounting_problem(tmp_path: Path) -> None:
    bundle = _bundle("clean", tmp_path)
    trials_path = bundle / "trials.jsonl"
    trials = _rows(trials_path)
    first_trial = trials[0]["trial"]
    trials.insert(1, dict(trials[0]))
    later_trial = next(row for row in trials if row["trial"] != first_trial)
    later_trial["requests"] += 1
    _write_rows(trials_path, trials)
    _refresh_manifest(bundle)

    problems = verify(bundle)

    assert Problem("trial_identity", first_trial, 1, 2) in problems
    assert any(
        problem.invariant == "proxy_accounting" and problem.subject == later_trial["trial"]
        for problem in problems
    )


def test_finished_bundle_reports_unjudged_proxy_trial(tmp_path: Path) -> None:
    bundle = _bundle("retries", tmp_path)
    events_path = bundle / "run-events.jsonl"
    events = _rows(events_path)
    events.append({"type": "finish", "exit_code": 3, "slots_complete": 2, "slots_incomplete": 2})
    _write_rows(events_path, events)
    _refresh_manifest(bundle)

    problems = verify(bundle)

    assert (
        Problem(
            "proxy_accounting",
            "unfinished",
            "trial row or documented non-trial id",
            "request without trial row",
        )
        in problems
    )


def test_finished_bundle_reporting_unjudged_trials_is_a_problem(tmp_path: Path) -> None:
    bundle = _bundle("clean", tmp_path)
    events_path = bundle / "run-events.jsonl"
    events = _rows(events_path)
    events.append({"type": "finish", "exit_code": 0, "slots_complete": 4, "slots_incomplete": 0})
    _write_rows(events_path, events)
    report_path = bundle / "report.md"
    report = report_path.read_text(encoding="utf-8")
    report_path.write_text(
        report.replace("| Trials not yet judged | 0 |", "| Trials not yet judged | 1 |"),
        encoding="utf-8",
    )
    _refresh_manifest(bundle)

    assert _problem_ids(verify(bundle)) == ["report_agreement"]


def test_unfinished_bundle_may_report_unjudged_trial_folders(tmp_path: Path) -> None:
    bundle = _bundle("clean", tmp_path)
    report_path = bundle / "report.md"
    report = report_path.read_text(encoding="utf-8")
    report_path.write_text(
        report.replace("| Trials not yet judged | 0 |", "| Trials not yet judged | 1 |"),
        encoding="utf-8",
    )
    _refresh_manifest(bundle)

    assert verify(bundle) == []


def test_manifest_mismatch_is_reported(tmp_path: Path) -> None:
    bundle = _bundle("clean", tmp_path)
    report_path = bundle / "report.md"
    report_path.write_text(
        report_path.read_text(encoding="utf-8").replace("# Run report", "# Changed report", 1),
        encoding="utf-8",
    )
    problems = verify(bundle)
    assert len(problems) == 2
    assert all(
        problem.invariant == "manifest"
        and problem.subject == "report.md"
        and problem.expected == "valid manifest"
        and problem.actual is not None
        for problem in problems
    )


def test_manifest_problem_subject_preserves_colons_in_file_names(tmp_path: Path) -> None:
    bundle = _bundle("clean", tmp_path)
    manifest_path = bundle / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"].append(
        {
            "name": "report:copy.md",
            "bytes": 0,
            "sha256": hashlib.sha256(b"").hexdigest(),
        }
    )
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    assert verify(bundle) == [
        Problem("manifest", "report", "valid manifest", "report:copy.md: missing file")
    ]


def test_resource_sample_count_disagreement_is_reported(tmp_path: Path) -> None:
    bundle = _bundle("clean", tmp_path)
    with (bundle / "resource-stats.jsonl.gz").open("wb") as stream:
        stream.write(gzip.compress(b'{"type":"sample","t":1}\nnot-json\n'))
    report_path = bundle / "report.md"
    report = report_path.read_text(encoding="utf-8")
    report_path.write_text(
        report.replace("| Malformed resource samples | 0 |", "| Malformed resource samples | 2 |")
    )
    _refresh_manifest(bundle)

    problems = verify(bundle)
    assert Problem("resource_samples", "resource-stats.jsonl.gz", 1, 2) in problems
    report_problem = next(
        problem for problem in problems if problem.invariant == "report_agreement"
    )
    assert report_problem.subject == "report.md"
    assert report_problem.expected["Malformed resource samples"] == 1
    assert report_problem.actual["Malformed resource samples"] == 2


def test_zero_sampler_interval_is_reported(tmp_path: Path) -> None:
    bundle = _bundle("clean", tmp_path)
    metadata_path = bundle / "run-metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["sampler_interval_s"] = 0
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    _refresh_manifest(bundle)

    assert verify(bundle) == [
        Problem("sampler_interval", "run-metadata.json", "positive number", 0)
    ]


def test_boolean_sampler_interval_is_reported(tmp_path: Path) -> None:
    bundle = _bundle("clean", tmp_path)
    metadata_path = bundle / "run-metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["sampler_interval_s"] = True
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    _refresh_manifest(bundle)

    assert verify(bundle) == [
        Problem("sampler_interval", "run-metadata.json", "positive number", True)
    ]


def test_default_sampler_interval_controls_host_sample_gaps(tmp_path: Path) -> None:
    bundle = _host_sample_bundle(
        tmp_path,
        [{"type": "sample", "t": 90}, {"type": "sample", "t": 121}],
    )
    metadata_path = bundle / "run-metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    del metadata["sampler_interval_s"]
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    _refresh_manifest(bundle)

    _assert_host_sample_result(bundle, damaged=True)


def test_invalid_sampler_interval_uses_ten_second_fallback(tmp_path: Path) -> None:
    bundle = _host_sample_bundle(
        tmp_path,
        [{"type": "sample", "t": 90}, {"type": "sample", "t": 121}],
    )
    metadata_path = bundle / "run-metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["sampler_interval_s"] = 0
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    _refresh_manifest(bundle)

    problems = verify(bundle)
    assert Problem("sampler_interval", "run-metadata.json", "positive number", 0) in problems
    assert any(problem.invariant == "host_samples_damaged" for problem in problems)


def test_harbor_start_reports_missing_telemetry_files(tmp_path: Path) -> None:
    bundle = _bundle("clean", tmp_path)
    events_path = bundle / "run-events.jsonl"
    events = _rows(events_path)
    events.append({"type": "harbor_start", "t": 1, "pass": 0})
    _write_rows(events_path, events)
    missing = ["resource-stats.jsonl.gz", "docker-oom-events.jsonl", "memory-watch.jsonl"]
    for name in missing:
        (bundle / name).unlink(missing_ok=True)
    _refresh_manifest(bundle)

    assert verify(bundle) == [
        Problem("telemetry_present", name, "present", "missing") for name in missing
    ]


def test_multiple_malformed_resource_rows_are_counted(tmp_path: Path) -> None:
    bundle = _bundle("clean", tmp_path)
    (bundle / "resource-stats.jsonl.gz").write_bytes(gzip.compress(b"[]\n1\n", mtime=0))
    report_path = bundle / "report.md"
    report = report_path.read_text(encoding="utf-8")
    report_path.write_text(
        report.replace("| Malformed resource samples | 0 |", "| Malformed resource samples | 2 |")
    )
    _refresh_manifest(bundle)

    assert verify(bundle) == []


def test_sampler_interval_checks_trial_sample_gaps(tmp_path: Path) -> None:
    bundle = _host_sample_bundle(
        tmp_path,
        [{"type": "sample", "t": 80}, {"type": "sample", "t": 140}],
    )

    _assert_host_sample_result(bundle, damaged=True)


def test_agent_only_trial_uses_agent_end_for_host_samples(tmp_path: Path) -> None:
    bundle = _host_sample_bundle(
        tmp_path,
        [{"type": "sample", "t": 100}, {"type": "sample", "t": 140}],
        verifier_started=None,
        verifier_finished=None,
    )

    _assert_host_sample_result(bundle, damaged=True)


def test_verifier_phase_host_sample_gap_is_reported(tmp_path: Path) -> None:
    bundle = _host_sample_bundle(
        tmp_path,
        [
            {"type": "sample", "t": 100},
            {"type": "sample", "t": 105},
            {"type": "sample", "t": 106},
            {"type": "sample", "t": 140},
        ],
        end=105,
        verifier_started=106,
        verifier_finished=150,
    )

    _assert_host_sample_result(bundle, damaged=True)


def test_verifier_phase_bad_host_sample_is_reported(tmp_path: Path) -> None:
    bundle = _host_sample_bundle(
        tmp_path,
        [
            {"type": "sample", "t": 100},
            {"type": "sample", "t": 105},
            {"type": "sample", "t": 106},
            b'{"type":',
            {"type": "sample", "t": 140},
            {"type": "sample", "t": 150},
        ],
        end=105,
        verifier_started=106,
        verifier_finished=150,
    )

    _assert_host_sample_result(bundle, damaged=True)


def test_host_samples_are_sorted_before_gap_check(tmp_path: Path) -> None:
    bundle = _host_sample_bundle(
        tmp_path,
        [{"type": "sample", "t": 140}, {"type": "sample", "t": 80}],
    )

    _assert_host_sample_result(bundle, damaged=True)


def test_host_sample_gap_at_three_intervals_is_accepted(tmp_path: Path) -> None:
    bundle = _host_sample_bundle(
        tmp_path,
        [{"type": "sample", "t": 90}, {"type": "sample", "t": 120}],
    )

    _assert_host_sample_result(bundle, damaged=False)


def test_host_sample_edge_at_three_intervals_is_accepted(tmp_path: Path) -> None:
    bundle = _host_sample_bundle(
        tmp_path,
        [
            {"type": "sample", "t": 70},
            {"type": "sample", "t": 100},
            {"type": "sample", "t": 110},
            {"type": "sample", "t": 140},
        ],
    )

    _assert_host_sample_result(bundle, damaged=False)


def test_host_sample_gap_beyond_window_edge_is_ignored(tmp_path: Path) -> None:
    bundle = _host_sample_bundle(
        tmp_path,
        [{"type": "sample", "t": 70}, {"type": "sample", "t": 80}],
    )

    _assert_host_sample_result(bundle, damaged=False)


@pytest.mark.parametrize(
    ("samples", "start", "end", "damaged"),
    [
        (
            [
                {"type": "sample", "t": 70},
                {"type": "sample", "t": 80},
                b'{"type":',
                {"type": "sample", "t": 90},
                {"type": "sample", "t": 100},
                {"type": "sample", "t": 110},
                {"type": "sample", "t": 120},
                {"type": "sample", "t": 130},
            ],
            100,
            110,
            False,
        ),
        (
            [
                {"type": "sample", "t": 90},
                {"type": "sample", "t": 100},
                {"type": "sample", "t": 110},
                b'{"type":',
                {"type": "sample", "t": 120},
                {"type": "sample", "t": 130},
                {"type": "sample", "t": 140},
            ],
            100,
            110,
            True,
        ),
        (
            [
                {"type": "sample", "t": 70},
                {"type": "sample", "t": 80},
                {"type": "sample", "t": 90},
                b'{"type":',
                {"type": "sample", "t": 100},
                {"type": "sample", "t": 110},
                {"type": "sample", "t": 120},
            ],
            100,
            110,
            True,
        ),
    ],
)
def test_malformed_resource_row_only_damages_overlapping_trial_window(
    samples: list[dict | bytes], start: int, end: int, damaged: bool, tmp_path: Path
) -> None:
    bundle = _host_sample_bundle(tmp_path, samples, start=start, end=end)

    _assert_host_sample_result(bundle, damaged=damaged)


def test_resource_sample_gap_line_numbers_are_preserved(tmp_path: Path) -> None:
    bundle = _host_sample_bundle(
        tmp_path,
        [{"type": "sample", "t": 90}, b'{"type":', {"type": "sample", "t": 120}],
    )

    _assert_host_sample_result(bundle, damaged=True)


def test_window_edge_beyond_three_intervals_from_any_sample_is_damage(tmp_path: Path) -> None:
    bundle = _host_sample_bundle(
        tmp_path,
        [{"type": "sample", "t": 50}, {"type": "sample", "t": 55}, {"type": "sample", "t": 60}],
    )

    _assert_host_sample_result(bundle, damaged=True)


def test_resource_row_that_is_not_an_object_is_a_bad_line(tmp_path: Path) -> None:
    bundle = _host_sample_bundle(
        tmp_path,
        [{"type": "sample", "t": 90}, b"[1]", {"type": "sample", "t": 120}],
    )

    _assert_host_sample_result(bundle, damaged=True)


def test_one_second_sampler_interval_is_not_replaced_by_default(tmp_path: Path) -> None:
    bundle = _host_sample_bundle(
        tmp_path,
        [
            {"type": "sample", "t": 97},
            {"type": "sample", "t": 100},
            {"type": "sample", "t": 105},
            {"type": "sample", "t": 110},
            {"type": "sample", "t": 113},
        ],
    )
    metadata_path = bundle / "run-metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["sampler_interval_s"] = 1
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    _refresh_manifest(bundle)

    _assert_host_sample_result(bundle, damaged=True)


def test_host_sample_gap_before_trial_window_is_ignored(tmp_path: Path) -> None:
    bundle = _host_sample_bundle(
        tmp_path,
        [{"type": "sample", "t": 50}, {"type": "sample", "t": 90}],
    )

    _assert_host_sample_result(bundle, damaged=False)


def test_host_sample_gap_after_trial_window_is_ignored(tmp_path: Path) -> None:
    bundle = _host_sample_bundle(
        tmp_path,
        [{"type": "sample", "t": 120}, {"type": "sample", "t": 160}],
    )

    _assert_host_sample_result(bundle, damaged=False)


def test_host_sample_gap_overlapping_trial_end_is_damaged(tmp_path: Path) -> None:
    bundle = _host_sample_bundle(
        tmp_path,
        [{"type": "sample", "t": 110}, {"type": "sample", "t": 150}],
    )

    _assert_host_sample_result(bundle, damaged=True)


def test_host_sample_gap_overlapping_trial_start_is_damaged(tmp_path: Path) -> None:
    bundle = _host_sample_bundle(
        tmp_path,
        [{"type": "sample", "t": 60}, {"type": "sample", "t": 100}],
    )

    _assert_host_sample_result(bundle, damaged=True)


def test_host_samples_ignore_boolean_timestamps(tmp_path: Path) -> None:
    bundle = _host_sample_bundle(
        tmp_path,
        [
            {"type": "sample", "t": 100},
            {"type": "sample", "t": 110},
            {"type": "sample", "t": True},
        ],
    )

    _assert_host_sample_result(bundle, damaged=False)


def test_host_sample_rows_with_other_types_do_not_cover_the_window(tmp_path: Path) -> None:
    bundle = _host_sample_bundle(
        tmp_path,
        [{"type": "event", "t": 100}, {"type": "event", "t": 110}],
    )

    _assert_host_sample_result(bundle, damaged=True)


def test_missing_host_samples_damage_measured_trial(tmp_path: Path) -> None:
    bundle = _host_sample_bundle(tmp_path, [])

    _assert_host_sample_result(bundle, damaged=True)


def test_invalid_trial_window_does_not_report_host_damage(tmp_path: Path) -> None:
    bundle = _host_sample_bundle(tmp_path, [], start=True)

    _assert_host_sample_result(bundle, damaged=False)


def test_invalid_trial_end_does_not_report_host_damage(tmp_path: Path) -> None:
    bundle = _host_sample_bundle(
        tmp_path,
        [],
        end=True,
        verifier_started=None,
        verifier_finished=None,
    )

    _assert_host_sample_result(bundle, damaged=False)


def test_trial_verdict_disagreement_is_reported(tmp_path: Path) -> None:
    bundle = _bundle("clean", tmp_path)
    rows = _rows(bundle / "trials.jsonl")
    rows[0]["measured"] = False
    _write_rows(bundle / "trials.jsonl", rows)
    verdict = next(
        event
        for event in _rows(bundle / "run-events.jsonl")
        if event.get("type") == "trial_verdict" and event["trial"] == rows[0]["trial"]
    )
    fields = ("valid", "measured", "reason", "unmeasured_reason")
    expected = {key: verdict.get(key) for key in fields}
    actual = {key: rows[0].get(key) for key in fields}
    _refresh_manifest(bundle)
    assert verify(bundle) == [Problem("trial_identity", rows[0]["trial"], expected, actual)]


def test_duplicate_trial_row_is_reported(tmp_path: Path) -> None:
    bundle = _bundle("clean", tmp_path)
    rows = _rows(bundle / "trials.jsonl")
    duplicate = dict(rows[0])
    duplicate["valid"] = False
    rows.append(duplicate)
    rows[1]["requests"] += 1
    _write_rows(bundle / "trials.jsonl", rows)
    compressed = gzip.decompress((bundle / "proxy-requests.jsonl.gz").read_bytes()).decode("utf-8")
    requests = [json.loads(line) for line in compressed.splitlines()]
    completed = [
        request
        for request in requests
        if request["trial"] == rows[1]["trial"] and request.get("upstream_status") is not None
    ]
    expected = {
        "requests": len(completed),
        "prompt_tokens": sum(request.get("prompt_tokens") or 0 for request in completed),
        "completion_tokens": sum(request.get("completion_tokens") or 0 for request in completed),
        "cached_read_tokens": sum(request.get("cached_read_tokens") or 0 for request in completed),
    }
    actual = {key: rows[1].get(key) for key in expected}
    _refresh_manifest(bundle)

    assert verify(bundle) == [
        Problem("trial_identity", rows[0]["trial"], 1, 2),
        Problem("proxy_accounting", rows[1]["trial"], expected, actual),
    ]


def test_missing_trial_verdict_is_reported(tmp_path: Path) -> None:
    bundle = _bundle("clean", tmp_path)
    trial = _rows(bundle / "trials.jsonl")[0]["trial"]
    events = _rows(bundle / "run-events.jsonl")
    events = [
        event
        for event in events
        if not (event.get("type") == "trial_verdict" and event["trial"] == trial)
    ]
    _write_rows(bundle / "run-events.jsonl", events)
    _refresh_manifest(bundle)

    assert verify(bundle) == [Problem("trial_identity", trial, 1, 0)]


def test_duplicate_trial_verdict_is_reported(tmp_path: Path) -> None:
    bundle = _bundle("clean", tmp_path)
    events = _rows(bundle / "run-events.jsonl")
    verdict = next(event for event in events if event.get("type") == "trial_verdict")
    events.append(dict(verdict))
    _write_rows(bundle / "run-events.jsonl", events)
    _refresh_manifest(bundle)

    assert verify(bundle) == [Problem("trial_identity", verdict["trial"], 1, 2)]


def test_verdict_without_trial_row_is_reported(tmp_path: Path) -> None:
    bundle = _bundle("clean", tmp_path)
    events = _rows(bundle / "run-events.jsonl")
    verdict = dict(next(event for event in events if event.get("type") == "trial_verdict"))
    verdict["trial"] = "unknown-trial"
    events.append(verdict)
    _write_rows(bundle / "run-events.jsonl", events)
    _refresh_manifest(bundle)

    assert verify(bundle) == [
        Problem("trial_identity", "unknown-trial", "trial row", "verdict without trial row")
    ]


def test_finished_torn_memory_watch_is_accepted_with_amendment(tmp_path: Path) -> None:
    bundle = _finished_telemetry_bundle(tmp_path, amendment=True)

    assert verify(bundle) == []


def test_finished_torn_memory_watch_requires_amendment(tmp_path: Path) -> None:
    bundle = _finished_telemetry_bundle(tmp_path, amendment=False)

    assert any(problem.invariant == "telemetry_damaged" for problem in verify(bundle))


def test_live_torn_memory_watch_does_not_require_amendment(tmp_path: Path) -> None:
    bundle = _finished_telemetry_bundle(tmp_path, amendment=False, stopped=False)

    assert verify(bundle) == []


def test_amendment_for_unknown_trial_is_reported(tmp_path: Path) -> None:
    bundle = _finished_telemetry_bundle(tmp_path, amendment=False, unknown_amendment=True)

    assert any(
        problem.invariant == "trial_identity" and problem.subject == "unknown-trial"
        for problem in verify(bundle)
    )


def test_telemetry_damage_and_null_peak_verify_from_memory_watch_copy(tmp_path: Path) -> None:
    bundle = _bundle("clean", tmp_path)
    trials_path = bundle / "trials.jsonl"
    trials = _rows(trials_path)
    trial = next(row for row in trials if row["location"] == "harbor-job")
    events_path = bundle / "run-events.jsonl"
    events = _rows(events_path)
    timed_trials = [row for row in trials if row["agent_started_t"] is not None]
    first = min(row["agent_started_t"] for row in timed_trials) + 1
    second = max(row["verifier_finished_t"] for row in timed_trials) + 1
    damaged = []
    for row in trials:
        end = (
            row["verifier_finished_t"]
            if row["verifier_started_t"] is not None
            else row["agent_finished_t"]
        )
        if (
            row["agent_started_t"] is not None
            and end is not None
            and first <= end
            and second >= row["agent_started_t"]
        ):
            row.update(measured=False, unmeasured_reason="telemetry_damaged", peak_memory_mb=None)
            damaged.append(row["trial"])
    _write_rows(trials_path, trials)
    for event in events:
        if event.get("trial") in damaged:
            event.update(measured=False, unmeasured_reason="telemetry_damaged")
    _write_rows(events_path, events)
    memory_lines = [
        json.dumps(
            {
                "type": "sample",
                "t": first,
                "trial": trial["trial"],
                "location": "harbor-job",
                "container": "container-a",
                "used_mb": 42,
                "limit_mb": 128,
                "reader": "docker_stats",
            },
            sort_keys=True,
        ),
        '{"t":',
        json.dumps(
            {
                "type": "sample",
                "t": second,
                "trial": trial["trial"],
                "location": "harbor-job",
                "container": "container-a",
                "used_mb": 48,
                "limit_mb": 128,
                "reader": "docker_stats",
            },
            sort_keys=True,
        ),
    ]
    (bundle / "memory-watch.jsonl").write_text("\n".join(memory_lines) + "\n")
    report_path = bundle / "report.md"
    report = report_path.read_text(encoding="utf-8")
    unmeasured = sum(row["location"] != "canary-job" and row["trial"] in damaged for row in trials)
    report = report.replace("| Unmeasured trials | 0 |", f"| Unmeasured trials | {unmeasured} |")
    report = report.replace(
        "| Tokens spent on set-aside trials |",
        "| Malformed memory-watch samples | 1 |\n| Tokens spent on set-aside trials |",
    )
    report_path.write_text(report, encoding="utf-8")
    _refresh_manifest(bundle)

    assert verify(bundle) == []


def test_oom_damage_disagreement_reports_trial_state(tmp_path: Path) -> None:
    bundle = _bundle("clean", tmp_path)
    trials_path = bundle / "trials.jsonl"
    trials = _rows(trials_path)
    harbor_trials = [row for row in trials if row["location"] == "harbor-job"]
    assert len(harbor_trials) >= 4
    windows = [
        (90, 95, 96, 100),
        (110, 105, 106, 120),
        (110.00000005, 105, 106, 120),
        (200, 205, 206, 210),
    ]
    damaged_trials = set()
    for row in trials:
        row.update(
            agent_started_t=None,
            agent_finished_t=None,
            verifier_started_t=None,
            verifier_finished_t=None,
        )
    for row, (start, agent_end, verifier_start, verifier_end) in zip(
        harbor_trials, windows, strict=True
    ):
        row.update(
            agent_started_t=start,
            agent_finished_t=agent_end,
            verifier_started_t=verifier_start,
            verifier_finished_t=verifier_end,
        )
        if row is harbor_trials[0]:
            row["peak_memory_mb"] = None
        if row is harbor_trials[1]:
            row.update(measured=False, unmeasured_reason="telemetry_damaged", peak_memory_mb=None)
            damaged_trials.add(row["trial"])
    _write_rows(trials_path, trials)
    events_path = bundle / "run-events.jsonl"
    events = _rows(events_path)
    for event in events:
        if event.get("trial") in damaged_trials:
            event.update(measured=False, unmeasured_reason="telemetry_damaged")
    _write_rows(events_path, events)
    oom_path = bundle / "docker-oom-events.jsonl"
    oom_path.write_text(
        json.dumps({"type": "oom", "timeNano": 100_000_000_000})
        + '\n{"type":'
        + "\n"
        + json.dumps({"type": "oom", "timeNano": 110_000_000_000})
        + "\n",
        encoding="utf-8",
    )
    report_path = bundle / "report.md"
    report = report_path.read_text(encoding="utf-8")
    report = report.replace("| Unmeasured trials | 0 |", "| Unmeasured trials | 1 |")
    report_path.write_text(
        report.replace(
            "| Tokens spent on set-aside trials |",
            "| Malformed Docker OOM events | 1 |\n| Tokens spent on set-aside trials |",
        ),
        encoding="utf-8",
    )
    _refresh_manifest(bundle)

    assert verify(bundle) == [
        Problem(
            "telemetry_damaged",
            harbor_trials[0]["trial"],
            {"measured": False, "unmeasured_reason": "telemetry_damaged"},
            {"measured": True, "unmeasured_reason": None},
        )
    ]


def test_telemetry_damage_uses_agent_end_without_verifier(tmp_path: Path) -> None:
    bundle = _bundle("clean", tmp_path)
    trials_path = bundle / "trials.jsonl"
    trials = _rows(trials_path)
    trial = next(row for row in trials if row["location"] == "harbor-job")
    for row in trials:
        row.update(
            agent_started_t=None,
            agent_finished_t=None,
            verifier_started_t=None,
            verifier_finished_t=None,
        )
    trial.update(
        agent_started_t=100,
        agent_finished_t=110,
        measured=False,
        unmeasured_reason="telemetry_damaged",
    )
    _write_rows(trials_path, trials)
    events_path = bundle / "run-events.jsonl"
    events = _rows(events_path)
    next(event for event in events if event.get("trial") == trial["trial"]).update(
        measured=False,
        unmeasured_reason="telemetry_damaged",
    )
    _write_rows(events_path, events)
    (bundle / "docker-oom-events.jsonl").write_text(
        '{"type":"oom","timeNano":105000000000}\n'
        '{"type":\n'
        '{"type":"oom","timeNano":106000000000}\n',
        encoding="utf-8",
    )
    report_path = bundle / "report.md"
    report = report_path.read_text(encoding="utf-8")
    report = report.replace("| Unmeasured trials | 0 |", "| Unmeasured trials | 1 |")
    report_path.write_text(
        report.replace(
            "| Tokens spent on set-aside trials |",
            "| Malformed Docker OOM events | 1 |\n| Tokens spent on set-aside trials |",
        ),
        encoding="utf-8",
    )
    _refresh_manifest(bundle)

    assert verify(bundle) == []


@pytest.mark.parametrize(
    ("start", "agent_end", "verifier_start", "verifier_end"),
    [(True, 110, None, None), (100, 105, 106, True)],
)
def test_boolean_trial_telemetry_times_do_not_mark_trial_damaged(
    start: object,
    agent_end: object,
    verifier_start: object,
    verifier_end: object,
    tmp_path: Path,
) -> None:
    bundle = _bundle("clean", tmp_path)
    trials_path = bundle / "trials.jsonl"
    trials = _rows(trials_path)
    trial = next(row for row in trials if row["location"] == "harbor-job")
    for row in trials:
        row.update(
            agent_started_t=None,
            agent_finished_t=None,
            verifier_started_t=None,
            verifier_finished_t=None,
        )
    trial.update(
        agent_started_t=start,
        agent_finished_t=agent_end,
        verifier_started_t=verifier_start,
        verifier_finished_t=verifier_end,
    )
    _write_rows(trials_path, trials)
    (bundle / "docker-oom-events.jsonl").write_text(
        '{"type":\n{"timeNano":100000000000000000000}\n', encoding="utf-8"
    )
    report_path = bundle / "report.md"
    report = report_path.read_text(encoding="utf-8")
    report_path.write_text(
        report.replace(
            "| Tokens spent on set-aside trials |",
            "| Malformed Docker OOM events | 1 |\n| Tokens spent on set-aside trials |",
        ),
        encoding="utf-8",
    )
    _refresh_manifest(bundle)

    assert verify(bundle) == []


def test_telemetry_damage_problem_reports_expected_and_actual_trial_state(
    tmp_path: Path,
) -> None:
    bundle = _bundle("clean", tmp_path)
    trials_path = bundle / "trials.jsonl"
    trials = _rows(trials_path)
    row = next(row for row in trials if row["location"] == "harbor-job")
    row.update(measured=False, unmeasured_reason="telemetry_damaged")
    _write_rows(trials_path, trials)
    events_path = bundle / "run-events.jsonl"
    events = _rows(events_path)
    next(event for event in events if event.get("trial") == row["trial"]).update(
        measured=False,
        unmeasured_reason="telemetry_damaged",
    )
    _write_rows(events_path, events)
    report_path = bundle / "report.md"
    report = report_path.read_text(encoding="utf-8")
    report_path.write_text(
        report.replace("| Unmeasured trials | 0 |", "| Unmeasured trials | 1 |"),
        encoding="utf-8",
    )
    _refresh_manifest(bundle)

    assert verify(bundle) == [
        Problem(
            "telemetry_damaged",
            row["trial"],
            {"measured": True, "unmeasured_reason": None},
            {"measured": False, "unmeasured_reason": "telemetry_damaged"},
        )
    ]


def test_set_aside_trial_must_be_invalid(tmp_path: Path) -> None:
    bundle = _bundle("retries", tmp_path)
    rows = _rows(bundle / "trials.jsonl")
    row = next(row for row in rows if row["location"] == "set-aside-trials")
    row["valid"] = True
    _write_rows(bundle / "trials.jsonl", rows)
    events_path = bundle / "run-events.jsonl"
    events = _rows(events_path)
    next(event for event in events if event.get("trial") == row["trial"])["valid"] = True
    _write_rows(events_path, events)
    _refresh_manifest(bundle)
    assert verify(bundle) == [Problem("locations", row["trial"], False, True)]


def test_arm_outside_metadata_arms_is_reported(tmp_path: Path) -> None:
    bundle = _bundle("clean", tmp_path)
    rows = _rows(bundle / "trials.jsonl")
    row = next(row for row in rows if row["location"] == "canary-job")
    row["arm"] = "arm/missing"
    _write_rows(bundle / "trials.jsonl", rows)
    _refresh_manifest(bundle)
    metadata = json.loads((bundle / "run-metadata.json").read_text(encoding="utf-8"))
    arm_names = sorted(arm["label"] for arm in metadata["arms"])
    assert verify(bundle) == [Problem("task_set", row["trial"], arm_names, "arm/missing")]


def test_set_aside_trial_task_outside_metadata_task_set_is_reported(tmp_path: Path) -> None:
    bundle = _bundle("retries", tmp_path)
    rows = _rows(bundle / "trials.jsonl")
    row = next(row for row in rows if row["location"] == "set-aside-trials")
    row["task"] = "task/missing"
    _write_rows(bundle / "trials.jsonl", rows)
    metadata = json.loads((bundle / "run-metadata.json").read_text(encoding="utf-8"))
    task_names = sorted(task["name"] for task in metadata["task_set"]["tasks"])
    _refresh_manifest(bundle)

    assert verify(bundle) == [Problem("task_set", row["trial"], task_names, "task/missing")]


def test_missing_task_set_is_reported(tmp_path: Path) -> None:
    bundle = _bundle("clean", tmp_path)
    metadata_path = bundle / "run-metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    del metadata["task_set"]
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    _refresh_manifest(bundle)

    problems = verify(bundle)
    assert Problem("task_set", "tasks", "non-empty task set", []) in problems


def test_missing_task_list_is_reported(tmp_path: Path) -> None:
    bundle = _bundle("clean", tmp_path)
    metadata_path = bundle / "run-metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    del metadata["task_set"]["tasks"]
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    _refresh_manifest(bundle)

    problems = verify(bundle)
    assert Problem("task_set", "tasks", "non-empty task set", []) in problems


def test_missing_arm_list_reports_unrecognized_trial_arm(tmp_path: Path) -> None:
    bundle = _bundle("clean", tmp_path)
    metadata_path = bundle / "run-metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    del metadata["arms"]
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    _refresh_manifest(bundle)
    row = _rows(bundle / "trials.jsonl")[0]

    problems = verify(bundle)
    assert Problem("task_set", row["trial"], [], row["arm"]) in problems


def test_report_slot_counts_are_recomputed(tmp_path: Path) -> None:
    bundle = _bundle("clean", tmp_path)
    report_path = bundle / "report.md"
    report_path.write_text(
        report_path.read_text(encoding="utf-8").replace(
            "| Slots planned | 4 |", "| Slots planned | 5 |"
        ),
        encoding="utf-8",
    )
    _refresh_manifest(bundle)
    assert _problem_ids(verify(bundle)) == ["report_agreement"]


def test_finish_counts_must_match_trials(tmp_path: Path) -> None:
    bundle = _bundle("clean", tmp_path)
    events_path = bundle / "run-events.jsonl"
    events = _rows(events_path)
    events.append({"type": "finish", "exit_code": 0, "slots_complete": 3, "slots_incomplete": 1})
    _write_rows(events_path, events)
    _refresh_manifest(bundle)
    assert verify(bundle) == [
        Problem(
            "finish_agreement",
            "finish",
            {"slots_complete": 4, "slots_incomplete": 0},
            {"slots_complete": 3, "slots_incomplete": 1},
        )
    ]


def test_finish_zero_exit_with_incomplete_slots_is_reported(tmp_path: Path) -> None:
    bundle = _bundle("clean", tmp_path)
    metadata_path = bundle / "run-metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["job"]["repetitions"] = 2
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    events_path = bundle / "run-events.jsonl"
    events = _rows(events_path)
    events.append({"type": "finish", "exit_code": 0, "slots_complete": 0, "slots_incomplete": 4})
    _write_rows(events_path, events)
    report_path = bundle / "report.md"
    report = report_path.read_text(encoding="utf-8")
    report = report.replace("| Slots complete | 4 |", "| Slots complete | 0 |")
    report_path.write_text(
        report.replace("| Slots incomplete | 0 |", "| Slots incomplete | 4 |"),
        encoding="utf-8",
    )
    _refresh_manifest(bundle)

    assert verify(bundle) == [Problem("finish_agreement", "finish.exit_code", 0, 4)]


def test_repetitions_count_valid_harbor_trials_per_slot(tmp_path: Path) -> None:
    bundle = _bundle("clean", tmp_path)
    metadata_path = bundle / "run-metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["job"]["repetitions"] = 2
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    trials_path = bundle / "trials.jsonl"
    trials = _rows(trials_path)
    original = next(row for row in trials if row["location"] == "harbor-job")
    duplicate = dict(original)
    duplicate["trial"] = f"{original['trial']}-repeat"
    duplicate.update(requests=0, prompt_tokens=0, completion_tokens=0, cached_read_tokens=0)
    trials.append(duplicate)
    _write_rows(trials_path, trials)
    events_path = bundle / "run-events.jsonl"
    events = _rows(events_path)
    verdict = dict(
        next(
            event
            for event in events
            if event.get("type") == "trial_verdict" and event["trial"] == original["trial"]
        )
    )
    verdict["trial"] = duplicate["trial"]
    events.append(verdict)
    _write_rows(events_path, events)
    report_path = bundle / "report.md"
    report = report_path.read_text(encoding="utf-8")
    report = report.replace("| Slots complete | 4 |", "| Slots complete | 1 |")
    report_path.write_text(
        report.replace("| Slots incomplete | 0 |", "| Slots incomplete | 3 |"),
        encoding="utf-8",
    )
    _refresh_manifest(bundle)

    assert verify(bundle) == []


def test_canary_trial_does_not_complete_a_main_slot(tmp_path: Path) -> None:
    bundle = _bundle("clean", tmp_path)
    metadata_path = bundle / "run-metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["job"]["repetitions"] = 2
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    trials_path = bundle / "trials.jsonl"
    trials = _rows(trials_path)
    main = next(row for row in trials if row["location"] == "harbor-job")
    canary = next(row for row in trials if row["location"] == "canary-job")
    canary.update(task=main["task"], arm=main["arm"], valid=True)
    _write_rows(trials_path, trials)
    events_path = bundle / "run-events.jsonl"
    events = _rows(events_path)
    verdict = next(event for event in events if event.get("trial") == canary["trial"])
    verdict.update(task=main["task"], arm=main["arm"], valid=True)
    _write_rows(events_path, events)
    report_path = bundle / "report.md"
    report = report_path.read_text(encoding="utf-8")
    report = report.replace("| Slots complete | 4 |", "| Slots complete | 0 |")
    report_path.write_text(
        report.replace("| Slots incomplete | 0 |", "| Slots incomplete | 4 |"),
        encoding="utf-8",
    )
    _refresh_manifest(bundle)

    assert verify(bundle) == []


def test_set_aside_cap_excludes_deferred_events(tmp_path: Path) -> None:
    bundle = _bundle("clean", tmp_path)
    metadata_path = bundle / "run-metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["limits"]["max_set_asides_per_slot"] = 0
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    events_path = bundle / "run-events.jsonl"
    events = _rows(events_path)
    events.extend(
        [
            {
                "type": "set_aside",
                "trial": "a-alpha-old",
                "task": "task/a",
                "arm": "alpha",
                "reason": "upstream_terminal_error",
            },
            {
                "type": "set_aside",
                "trial": "another",
                "task": "task/a",
                "arm": "alpha",
                "reason": "deferred",
            },
        ]
    )
    _write_rows(events_path, events)
    _refresh_manifest(bundle)
    assert verify(bundle) == [Problem("set_aside_cap", "task/a/alpha", 0, 1)]


def test_set_aside_cap_allows_count_at_limit(tmp_path: Path) -> None:
    bundle = _bundle("clean", tmp_path)
    metadata_path = bundle / "run-metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["limits"]["max_set_asides_per_slot"] = 1
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    task = metadata["task_set"]["tasks"][0]["name"]
    arm = metadata["arms"][0]["label"]
    events_path = bundle / "run-events.jsonl"
    events = _rows(events_path)
    events.append(
        {
            "type": "set_aside",
            "trial": "a-retry",
            "task": task,
            "arm": arm,
            "reason": "upstream_terminal_error",
        }
    )
    _write_rows(events_path, events)
    _refresh_manifest(bundle)

    assert verify(bundle) == []


def test_exempt_set_aside_event_does_not_count_toward_cap(tmp_path: Path) -> None:
    bundle = _bundle("clean", tmp_path)
    metadata_path = bundle / "run-metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["limits"]["max_set_asides_per_slot"] = 0
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    events_path = bundle / "run-events.jsonl"
    events = _rows(events_path)
    events.append(
        {
            "type": "set_aside",
            "trial": "deferred-trial",
            "task": metadata["task_set"]["tasks"][0]["name"],
            "arm": metadata["arms"][0]["label"],
            "reason": "deferred",
        }
    )
    _write_rows(events_path, events)
    _refresh_manifest(bundle)

    assert verify(bundle) == []


def test_default_set_aside_cap_scales_with_repetitions(tmp_path: Path) -> None:
    bundle = _bundle("clean", tmp_path)
    metadata_path = bundle / "run-metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["job"]["repetitions"] = 2
    del metadata["limits"]
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    task = metadata["task_set"]["tasks"][0]["name"]
    arm = metadata["arms"][0]["label"]
    events_path = bundle / "run-events.jsonl"
    events = _rows(events_path)
    events.extend(
        {
            "type": "set_aside",
            "trial": f"retry-{index}",
            "task": task,
            "arm": arm,
            "reason": "upstream_terminal_error",
        }
        for index in range(5)
    )
    _write_rows(events_path, events)
    report_path = bundle / "report.md"
    report = report_path.read_text(encoding="utf-8")
    report = report.replace("| Slots complete | 4 |", "| Slots complete | 0 |")
    report_path.write_text(
        report.replace("| Slots incomplete | 0 |", "| Slots incomplete | 4 |"),
        encoding="utf-8",
    )
    _refresh_manifest(bundle)

    assert verify(bundle) == [Problem("set_aside_cap", f"{task}/{arm}", 4, 5)]


def test_memory_peak_must_match_samples(tmp_path: Path) -> None:
    bundle = _bundle("clean", tmp_path)
    rows = _rows(bundle / "trials.jsonl")
    rows[0]["peak_memory_mb"] = None
    _write_rows(bundle / "trials.jsonl", rows)
    (bundle / "memory-watch.jsonl").write_text(
        json.dumps(
            {"type": "sample", "trial": rows[0]["trial"], "location": "harbor-job", "used_mb": 42}
        )
        + "\n",
        encoding="utf-8",
    )
    _refresh_manifest(bundle)
    problems = verify(bundle)
    assert _problem_ids(problems) == ["report_agreement", "memory"]
    assert Problem("memory", rows[0]["trial"], 42, None) in problems


def test_memory_peak_disagreement_preserves_non_null_trial_value(tmp_path: Path) -> None:
    bundle = _bundle("clean", tmp_path)
    trials = _rows(bundle / "trials.jsonl")
    row = trials[0]
    row["peak_memory_mb"] = 41
    _write_rows(bundle / "trials.jsonl", trials)
    (bundle / "memory-watch.jsonl").write_text(
        json.dumps(
            {"type": "sample", "trial": row["trial"], "location": "harbor-job", "used_mb": 42}
        )
        + "\n",
        encoding="utf-8",
    )
    _refresh_manifest(bundle)

    assert Problem("memory", row["trial"], 42, 41) in verify(bundle)


def test_non_sample_memory_rows_do_not_hide_later_samples(tmp_path: Path) -> None:
    bundle = _bundle("clean", tmp_path)
    trials = _rows(bundle / "trials.jsonl")
    row = trials[0]
    row["peak_memory_mb"] = 42
    _write_rows(bundle / "trials.jsonl", trials)
    (bundle / "memory-watch.jsonl").write_text(
        json.dumps({"type": "over_limit", "trial": row["trial"]})
        + "\n"
        + json.dumps(
            {"type": "sample", "trial": row["trial"], "location": "harbor-job", "used_mb": 42}
        )
        + "\n",
        encoding="utf-8",
    )
    report_path = bundle / "report.md"
    report = report_path.read_text(encoding="utf-8")
    report_path.write_text(
        report.replace(
            "| Tokens spent on set-aside trials |",
            "| Malformed memory-watch samples | 0 |\n| Tokens spent on set-aside trials |",
        ),
        encoding="utf-8",
    )
    _refresh_manifest(bundle)

    assert verify(bundle) == []


def test_canary_memory_sample_is_included_in_peak_check(tmp_path: Path) -> None:
    bundle = _bundle("clean", tmp_path)
    canary = next(row for row in _rows(bundle / "trials.jsonl") if row["location"] == "canary-job")
    canary["peak_memory_mb"] = None
    trials = _rows(bundle / "trials.jsonl")
    next(row for row in trials if row["trial"] == canary["trial"])["peak_memory_mb"] = None
    _write_rows(bundle / "trials.jsonl", trials)
    (bundle / "memory-watch.jsonl").write_text(
        json.dumps(
            {
                "type": "over_limit",
                "trial": canary["trial"],
                "location": "canary-job",
                "container": "container-a",
            }
        )
        + "\n"
        + json.dumps(
            {
                "type": "sample",
                "trial": canary["trial"],
                "location": "canary-job",
                "container": "container-a",
                "used_mb": 42,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    report_path = bundle / "report.md"
    report = report_path.read_text(encoding="utf-8")
    report_path.write_text(
        report.replace(
            "| Tokens spent on set-aside trials |",
            "| Malformed memory-watch samples | 0 |\n| Tokens spent on set-aside trials |",
        ),
        encoding="utf-8",
    )
    _refresh_manifest(bundle)

    problems = verify(bundle)
    assert Problem("memory", canary["trial"], 42, None) in problems


def test_proxy_usage_must_match_request_rows(tmp_path: Path) -> None:
    bundle = _bundle("clean", tmp_path)
    rows = _rows(bundle / "trials.jsonl")
    rows[0]["requests"] += 1
    _write_rows(bundle / "trials.jsonl", rows)
    requests_path = bundle / "proxy-requests.jsonl.gz"
    compressed = gzip.decompress(requests_path.read_bytes()).decode("utf-8")
    requests = [json.loads(line) for line in compressed.splitlines()]
    nullable = next(
        request
        for request in requests
        if request["trial"] == rows[0]["trial"] and request.get("upstream_status") is not None
    )
    nullable["prompt_tokens"] = None
    nullable["completion_tokens"] = None
    raw = "".join(json.dumps(request, sort_keys=True) + "\n" for request in requests).encode(
        "utf-8"
    )
    requests_path.write_bytes(gzip.compress(raw, mtime=0))
    _refresh_manifest(bundle)
    compressed = gzip.decompress((bundle / "proxy-requests.jsonl.gz").read_bytes()).decode("utf-8")
    requests = [json.loads(line) for line in compressed.splitlines()]
    completed = [
        request
        for request in requests
        if request["trial"] == rows[0]["trial"] and request.get("upstream_status") is not None
    ]
    expected = {
        "requests": len(completed),
        "prompt_tokens": sum(request.get("prompt_tokens") or 0 for request in completed),
        "completion_tokens": sum(request.get("completion_tokens") or 0 for request in completed),
        "cached_read_tokens": sum(request.get("cached_read_tokens") or 0 for request in completed),
    }
    actual = {key: rows[0].get(key) for key in expected}
    assert verify(bundle) == [Problem("proxy_accounting", rows[0]["trial"], expected, actual)]


def test_measured_trial_cannot_have_unfinished_request(tmp_path: Path) -> None:
    bundle = _bundle("clean", tmp_path)
    requests_path = bundle / "proxy-requests.jsonl.gz"
    existing = gzip.decompress(requests_path.read_bytes()).decode("utf-8")
    rows = [json.loads(line) for line in existing.splitlines()]
    trial = _rows(bundle / "trials.jsonl")[0]["trial"]
    rows.append(
        {
            "trial": trial,
            "request_id": "unfinished",
            "seq": 1000,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "cached_read_tokens": 0,
            "upstream_status": None,
        }
    )
    raw = "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows).encode("utf-8")
    requests_path.write_bytes(gzip.compress(raw, mtime=0))
    _refresh_manifest(bundle)
    assert verify(bundle) == [Problem("measured_closed", trial, "no unfinished requests", 1)]


@pytest.mark.parametrize("filename", ["trials.jsonl", "run-events.jsonl"])
def test_one_malformed_identity_jsonl_file_is_rejected(filename: str, tmp_path: Path) -> None:
    bundle = _bundle("clean", tmp_path)
    path = bundle / filename
    lines = path.read_bytes().splitlines()
    path.write_bytes(b"\n".join([lines[0], b"not-json", *lines[1:]]) + b"\n")

    with pytest.raises(ValueError, match="bundle contains malformed JSONL records"):
        verify(bundle)
