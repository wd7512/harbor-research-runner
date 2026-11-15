from __future__ import annotations

import gzip
import hashlib
import json
import shutil
from pathlib import Path

from harbor_research_runner.evidence_bundle.build import build
from harbor_research_runner.evidence_bundle.verify import verify
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


def _host_sample_bundle(
    tmp_path: Path,
    samples: list[dict],
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
    resource_rows = [
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
        }
        for sample in samples
    ]
    resource_data = "".join(json.dumps(sample, sort_keys=True) + "\n" for sample in resource_rows)
    (bundle / "resource-stats.jsonl.gz").write_bytes(
        gzip.compress(resource_data.encode("utf-8"), mtime=0)
    )
    _refresh_manifest(bundle)
    return bundle


def _problem_ids(problems) -> list[str]:
    return [problem.invariant for problem in problems]


def test_clean_and_retry_bundles_verify(tmp_path: Path) -> None:
    assert verify(_bundle("clean", tmp_path)) == []
    assert verify(_bundle("retries", tmp_path)) == []


def test_finished_bundle_reports_unjudged_proxy_trial(tmp_path: Path) -> None:
    bundle = _bundle("retries", tmp_path)
    events_path = bundle / "run-events.jsonl"
    events = _rows(events_path)
    events.append({"type": "finish", "exit_code": 3, "slots_complete": 2, "slots_incomplete": 2})
    _write_rows(events_path, events)
    _refresh_manifest(bundle)

    problems = verify(bundle)

    assert any(
        problem.invariant == "proxy_accounting" and problem.subject == "unfinished"
        for problem in problems
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
    assert set(_problem_ids(verify(bundle))) == {"manifest"}


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

    assert "resource_samples" in _problem_ids(verify(bundle))


def test_sampler_interval_checks_trial_sample_gaps(tmp_path: Path) -> None:
    bundle = _bundle("clean", tmp_path)
    (bundle / "resource-stats.jsonl.gz").write_bytes(
        gzip.compress(b'{"type":"sample","t":1767225600}\n{"type":"sample","t":1767225640}\n')
    )
    events_path = bundle / "run-events.jsonl"
    events = _rows(events_path)
    events.append({"type": "harbor_start", "t": 1, "pass": 0})
    _write_rows(events_path, events)
    _refresh_manifest(bundle)

    assert "host_samples_damaged" in _problem_ids(verify(bundle))


def test_agent_only_trial_uses_agent_end_for_host_samples(tmp_path: Path) -> None:
    bundle = _host_sample_bundle(
        tmp_path,
        [{"type": "sample", "t": 100}, {"type": "sample", "t": 140}],
        verifier_started=None,
        verifier_finished=None,
    )

    assert "host_samples_damaged" in _problem_ids(verify(bundle))


def test_host_samples_are_sorted_before_gap_check(tmp_path: Path) -> None:
    bundle = _host_sample_bundle(
        tmp_path,
        [{"type": "sample", "t": 140}, {"type": "sample", "t": 80}],
    )

    assert "host_samples_damaged" in _problem_ids(verify(bundle))


def test_host_sample_gap_at_three_intervals_is_accepted(tmp_path: Path) -> None:
    bundle = _host_sample_bundle(
        tmp_path,
        [{"type": "sample", "t": 90}, {"type": "sample", "t": 120}],
    )

    assert "host_samples_damaged" not in _problem_ids(verify(bundle))


def test_host_sample_gap_before_trial_window_is_ignored(tmp_path: Path) -> None:
    bundle = _host_sample_bundle(
        tmp_path,
        [{"type": "sample", "t": 50}, {"type": "sample", "t": 90}],
    )

    assert "host_samples_damaged" not in _problem_ids(verify(bundle))


def test_host_sample_gap_after_trial_window_is_ignored(tmp_path: Path) -> None:
    bundle = _host_sample_bundle(
        tmp_path,
        [{"type": "sample", "t": 120}, {"type": "sample", "t": 160}],
    )

    assert "host_samples_damaged" not in _problem_ids(verify(bundle))


def test_host_sample_gap_overlapping_trial_end_is_damaged(tmp_path: Path) -> None:
    bundle = _host_sample_bundle(
        tmp_path,
        [{"type": "sample", "t": 110}, {"type": "sample", "t": 150}],
    )

    assert "host_samples_damaged" in _problem_ids(verify(bundle))


def test_host_sample_gap_overlapping_trial_start_is_damaged(tmp_path: Path) -> None:
    bundle = _host_sample_bundle(
        tmp_path,
        [{"type": "sample", "t": 60}, {"type": "sample", "t": 100}],
    )

    assert "host_samples_damaged" in _problem_ids(verify(bundle))


def test_host_samples_ignore_boolean_timestamps(tmp_path: Path) -> None:
    bundle = _host_sample_bundle(
        tmp_path,
        [
            {"type": "sample", "t": 100},
            {"type": "sample", "t": 110},
            {"type": "sample", "t": True},
        ],
    )

    assert "host_samples_damaged" not in _problem_ids(verify(bundle))


def test_host_sample_rows_with_other_types_do_not_cover_the_window(tmp_path: Path) -> None:
    bundle = _host_sample_bundle(
        tmp_path,
        [{"type": "event", "t": 100}, {"type": "event", "t": 110}],
    )

    assert "host_samples_damaged" in _problem_ids(verify(bundle))


def test_missing_host_samples_damage_measured_trial(tmp_path: Path) -> None:
    bundle = _host_sample_bundle(tmp_path, [])

    assert "host_samples_damaged" in _problem_ids(verify(bundle))


def test_invalid_trial_window_does_not_report_host_damage(tmp_path: Path) -> None:
    bundle = _host_sample_bundle(tmp_path, [], start=True)

    assert "host_samples_damaged" not in _problem_ids(verify(bundle))


def test_invalid_trial_end_does_not_report_host_damage(tmp_path: Path) -> None:
    bundle = _host_sample_bundle(
        tmp_path,
        [],
        end=True,
        verifier_started=None,
        verifier_finished=None,
    )

    assert "host_samples_damaged" not in _problem_ids(verify(bundle))


def test_trial_verdict_disagreement_is_reported(tmp_path: Path) -> None:
    bundle = _bundle("clean", tmp_path)
    rows = _rows(bundle / "trials.jsonl")
    rows[0]["measured"] = False
    _write_rows(bundle / "trials.jsonl", rows)
    _refresh_manifest(bundle)
    assert _problem_ids(verify(bundle)) == ["trial_identity"]


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
    assert _problem_ids(verify(bundle)) == ["locations"]


def test_arm_outside_metadata_arms_is_reported(tmp_path: Path) -> None:
    bundle = _bundle("clean", tmp_path)
    rows = _rows(bundle / "trials.jsonl")
    row = next(row for row in rows if row["location"] == "canary-job")
    row["arm"] = "arm/missing"
    _write_rows(bundle / "trials.jsonl", rows)
    _refresh_manifest(bundle)
    assert _problem_ids(verify(bundle)) == ["task_set"]


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
    assert _problem_ids(verify(bundle)) == ["finish_agreement"]


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
    assert _problem_ids(verify(bundle)) == ["set_aside_cap"]


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
    assert set(_problem_ids(verify(bundle))) == {"report_agreement", "memory"}


def test_proxy_usage_must_match_request_rows(tmp_path: Path) -> None:
    bundle = _bundle("clean", tmp_path)
    rows = _rows(bundle / "trials.jsonl")
    rows[0]["requests"] += 1
    _write_rows(bundle / "trials.jsonl", rows)
    _refresh_manifest(bundle)
    assert _problem_ids(verify(bundle)) == ["proxy_accounting"]


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
    assert _problem_ids(verify(bundle)) == ["measured_closed"]
