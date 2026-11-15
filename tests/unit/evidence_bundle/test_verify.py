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


def test_manifest_mismatch_is_reported(tmp_path: Path) -> None:
    bundle = _bundle("clean", tmp_path)
    report_path = bundle / "report.md"
    report_path.write_text(
        report_path.read_text(encoding="utf-8").replace("# Run report", "# Changed report", 1),
        encoding="utf-8",
    )
    assert set(_problem_ids(verify(bundle))) == {"manifest"}


def test_trial_verdict_disagreement_is_reported(tmp_path: Path) -> None:
    bundle = _bundle("clean", tmp_path)
    rows = _rows(bundle / "trials.jsonl")
    rows[0]["measured"] = False
    _write_rows(bundle / "trials.jsonl", rows)
    _refresh_manifest(bundle)
    assert _problem_ids(verify(bundle)) == ["trial_identity"]


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
    assert _problem_ids(verify(bundle)) == ["memory"]


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
