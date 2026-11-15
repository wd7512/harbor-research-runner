from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

from harbor_research_runner.evidence_bundle.build import build
from harbor_research_runner.records.manifest import verify_manifest
from harbor_research_runner.records.trials import TrialRow
from harbor_research_runner.results_explorer.loader import EvidenceStore, discover
from harbor_research_runner.statistics.completeness import completeness
from harbor_research_runner.statistics.rates import (
    arm_rates,
    pass_rate_over_time,
    pass_rate_over_time_by_arm,
    task_arm_table,
)
from harbor_research_runner.statistics.usage import label_counts

FIXTURES = Path(__file__).parents[2] / "fixtures" / "run_folders"


def _bundle(source: str, root: Path) -> Path:
    run_dir = root / f"run-{source}"
    shutil.copytree(FIXTURES / source, run_dir)
    metadata_path = run_dir / "run-metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["machine_check_sha256"] = hashlib.sha256(
        (run_dir / "machine-check.json").read_bytes()
    ).hexdigest()
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    bundle = root / f"bundle-{source}"
    assert build(run_dir, bundle, {}) == 0
    assert verify_manifest(bundle) == []
    return bundle


def test_discover_finds_bundles_through_two_directory_levels(tmp_path: Path) -> None:
    nested = tmp_path / "group"
    nested.mkdir(parents=True)
    first = _bundle("clean", tmp_path)
    second = _bundle("retries", nested)

    assert discover([tmp_path]) == [first, second]


def test_summary_uses_statistics_for_bundle_rows(tmp_path: Path) -> None:
    bundle = _bundle("clean", tmp_path)
    store = EvidenceStore([bundle])
    rows = [TrialRow.from_row(row) for row in store.trials("clean")]
    metadata = json.loads((bundle / "run-metadata.json").read_text(encoding="utf-8"))
    planned = [
        (task["name"], arm["label"])
        for task in metadata["task_set"]["tasks"]
        for arm in metadata["arms"]
    ]

    summary = store.summary("clean")
    for view in ("first_attempt", "final", "final_excluding_reruns"):
        assert summary["rates"][view] == {
            arm: rate.__dict__ for arm, rate in arm_rates(rows, view).items()
        }
        assert summary["tasks"][view] == {
            f"{task}|{arm}": list(counts)
            for (task, arm), counts in task_arm_table(rows, view).items()
        }
    assert (
        summary["completeness"]
        == completeness(rows, planned, metadata["job"]["repetitions"]).__dict__
    )
    assert summary["labels_by_arm"] == {
        arm["label"]: label_counts(rows, arm["label"]) for arm in metadata["arms"]
    }
    assert summary["over_time"] == [bucket.__dict__ for bucket in pass_rate_over_time(rows)]

    over_time = store.over_time("clean")
    assert over_time == {
        "all": [bucket.__dict__ for bucket in pass_rate_over_time(rows)],
        "by_arm": {
            arm: [bucket.__dict__ for bucket in buckets]
            for arm, buckets in pass_rate_over_time_by_arm(rows).items()
        },
    }


def test_missing_proxy_requests_file_refuses_bundle_with_problem(tmp_path: Path) -> None:
    bundle = _bundle("clean", tmp_path)
    (bundle / "proxy-requests.jsonl.gz").unlink()

    try:
        EvidenceStore([bundle]).summary("clean")
    except ValueError as exc:
        assert "proxy-requests.jsonl.gz: missing file" in str(exc)
    else:
        raise AssertionError("bundle with a missing request file must be refused")


def test_changed_proxy_requests_file_refuses_bundle_with_problem(tmp_path: Path) -> None:
    bundle = _bundle("clean", tmp_path)
    requests_path = bundle / "proxy-requests.jsonl.gz"
    requests_path.write_bytes(requests_path.read_bytes() + b"x")

    try:
        EvidenceStore([bundle]).summary("clean")
    except ValueError as exc:
        assert "proxy-requests.jsonl.gz: hash mismatch" in str(exc)
    else:
        raise AssertionError("bundle with a changed request file must be refused")


def test_trial_filters_apply_arm_task_and_validity(tmp_path: Path) -> None:
    bundle = _bundle("retries", tmp_path)
    store = EvidenceStore([bundle])

    rows = store.trials("retries", arm="alpha", task="task/a", valid=True)

    assert rows
    assert all(row["arm"] == "alpha" and row["task"] == "task/a" and row["valid"] for row in rows)


def test_compare_pairs_tasks_across_bundles_by_name(tmp_path: Path) -> None:
    clean = _bundle("clean", tmp_path)
    retries = _bundle("retries", tmp_path)
    store = EvidenceStore([clean, retries])

    result = store.compare(["clean", "retries"], "alpha", "beta", "final")

    assert result["tasks"] == [
        {"task": "task/a", "arm_a": [1, 1], "arm_b": None},
        {"task": "task/b", "arm_a": [1, 1], "arm_b": [1, 1]},
    ]
    assert result["paired_difference"]["tasks"] == 1
    assert result["paired_difference"]["unpaired_tasks"] == 1
    assert result["paired_difference"]["per_task"] == (
        {"task": "task/b", "rate_a": 1.0, "rate_b": 1.0, "difference": 0.0},
    )


def test_compare_uses_only_the_selected_bundle_and_arm(tmp_path: Path) -> None:
    bundle_a = _bundle("clean", tmp_path)
    bundle_b = tmp_path / "bundle-b"
    shutil.copytree(bundle_a, bundle_b)
    manifest_path = bundle_b / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["run_id"] = "bundle-b"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    trials_path = bundle_b / "trials.jsonl"
    rows = [json.loads(line) for line in trials_path.read_text(encoding="utf-8").splitlines()]
    for row in rows:
        if row["location"] == "harbor-job":
            row["reward"] = 0.0
            row["is_pass"] = False
    trials_path.write_text(
        "".join(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n" for row in rows),
        encoding="utf-8",
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    trials_record = next(item for item in manifest["files"] if item["name"] == "trials.jsonl")
    trials_data = trials_path.read_bytes()
    trials_record["bytes"] = len(trials_data)
    trials_record["sha256"] = hashlib.sha256(trials_data).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    store = EvidenceStore([bundle_a, bundle_b])

    result = store.compare(["clean", "bundle-b"], "alpha", "beta", "final")

    assert result["tasks"] == [
        {"task": "task/a", "arm_a": [1, 1], "arm_b": [0, 1]},
        {"task": "task/b", "arm_a": [1, 1], "arm_b": [0, 1]},
    ]
    assert result["paired_difference"]["mean_difference"] == 1.0
