from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import asdict
from pathlib import Path

from harbor_research_runner.evidence_bundle.build import build
from harbor_research_runner.evidence_bundle.manifest import verify_manifest
from harbor_research_runner.records.trials import TrialRow
from harbor_research_runner.results_explorer.loader import EvidenceStore, discover
from harbor_research_runner.statistics.completeness import completeness
from harbor_research_runner.statistics.paired import paired_difference
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
    combined = [TrialRow.from_row(row) for row in store.trials("clean") + store.trials("retries")]
    expected = paired_difference(combined, "final", "alpha", "beta")

    assert result["paired_difference"] == asdict(expected)
    assert result["paired_difference"]["per_task"] == tuple(
        task.__dict__ for task in expected.per_task
    )
    assert {row["task"] for row in result["tasks"]} == {
        task for task, _arm in task_arm_table(combined, "final")
    }
