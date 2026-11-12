from __future__ import annotations

import gzip
import hashlib
import json
import logging
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path

from harbor_research_runner.records.jsonl import read_rows
from harbor_research_runner.records.trials import TrialRow
from harbor_research_runner.statistics.completeness import completeness
from harbor_research_runner.statistics.paired import paired_difference
from harbor_research_runner.statistics.rates import (
    arm_rates,
    pass_rate_over_time,
    pass_rate_over_time_by_arm,
    task_arm_table,
)
from harbor_research_runner.statistics.usage import label_counts

logger = logging.getLogger(__name__)


def discover(paths: Sequence[Path]) -> list[Path]:
    found: set[Path] = set()
    for raw in paths:
        root = Path(raw)
        if _is_bundle(root):
            found.add(root)
            continue
        if not root.exists():
            continue
        if not root.is_dir():
            continue
        for candidate in (root, *root.glob("*"), *root.glob("*/*")):
            if _is_bundle(candidate):
                found.add(candidate)
    return sorted(found)


def _is_bundle(path: Path) -> bool:
    return (
        path.is_dir() and (path / "manifest.json").is_file() and (path / "trials.jsonl").is_file()
    )


class EvidenceStore:
    def __init__(self, paths: Sequence[Path]):
        self.paths = discover(paths)
        self._cache: dict[str, tuple[str, dict]] = {}

    def _load(self, run_id: str) -> dict:
        for path in self.paths:
            try:
                bundle = self._load_for_path(path)
            except OSError:
                continue
            if bundle["manifest"].get("run_id") == run_id:
                return bundle
        raise KeyError(run_id)

    @staticmethod
    def _read_bundle(path: Path) -> dict:
        manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
        metadata = json.loads((path / "run-metadata.json").read_text(encoding="utf-8"))
        rows = read_rows(path / "trials.jsonl").rows
        events_path = path / "run-events.jsonl"
        events = read_rows(events_path).rows if events_path.is_file() else []
        requests_path = path / "proxy-requests.jsonl.gz"
        if requests_path.is_file():
            with gzip.open(requests_path, "rt", encoding="utf-8") as stream:
                requests = [json.loads(line) for line in stream if line.strip()]
        else:
            requests = []
        return {
            "path": path,
            "manifest": manifest,
            "metadata": metadata,
            "rows": [TrialRow.from_row(row) for row in rows],
            "events": events,
            "requests": requests,
        }

    def evidence_bundles(self) -> list[dict]:
        result = []
        for path in self.paths:
            try:
                bundle = self._load_for_path(path)
            except (OSError, ValueError, KeyError, TypeError) as exc:
                logger.warning("skipping unreadable evidence bundle %s: %s", path, exc)
                continue
            metadata = bundle["metadata"]
            finish = next(
                (event for event in reversed(bundle["events"]) if event.get("type") == "finish"),
                None,
            )
            result.append(
                {
                    "run_id": bundle["manifest"].get("run_id"),
                    "model": metadata.get("model_card", {}).get("id"),
                    "arms": metadata.get("arms", []),
                    "tasks": [
                        task["name"] for task in metadata.get("task_set", {}).get("tasks", [])
                    ],
                    "created_t": metadata.get("created_t"),
                    "exit_code": finish.get("exit_code") if finish else None,
                }
            )
        return result

    def _load_for_path(self, path: Path) -> dict:
        manifest_hash = hashlib.sha256((path / "manifest.json").read_bytes()).hexdigest()
        cache_key = str(path)
        cached = self._cache.get(cache_key)
        if cached is not None and cached[0] == manifest_hash:
            return cached[1]
        bundle = self._read_bundle(path)
        self._cache[cache_key] = (manifest_hash, bundle)
        return bundle

    def summary(self, run_id: str) -> dict:
        bundle = self._load(run_id)
        rows = bundle["rows"]
        metadata = bundle["metadata"]
        planned_slots = [
            (task["name"], arm["label"])
            for task in metadata.get("task_set", {}).get("tasks", [])
            for arm in metadata.get("arms", [])
        ]
        views = ("first_attempt", "final", "final_excluding_reruns")
        arms = [arm["label"] for arm in metadata.get("arms", [])]
        return {
            "run_id": run_id,
            "rates": {
                view: {arm: asdict(rate) for arm, rate in arm_rates(rows, view).items()}
                for view in views
            },
            "tasks": {
                view: {
                    f"{task}|{arm}": list(counts)
                    for (task, arm), counts in task_arm_table(rows, view).items()
                }
                for view in views
            },
            "completeness": asdict(
                completeness(rows, planned_slots, metadata.get("job", {}).get("repetitions", 1))
            ),
            "labels_by_arm": {arm: label_counts(rows, arm) for arm in arms},
            "over_time": [asdict(bucket) for bucket in pass_rate_over_time(rows)],
        }

    def trials(
        self,
        run_id: str,
        arm: str | None = None,
        task: str | None = None,
        valid: bool | None = None,
    ) -> list[dict]:
        rows = self._load(run_id)["rows"]
        return [
            row.to_row()
            for row in rows
            if (arm is None or row.arm == arm)
            and (task is None or row.task == task)
            and (valid is None or row.valid is valid)
        ]

    def requests(self, run_id: str, trial: str) -> list[dict]:
        return [row for row in self._load(run_id)["requests"] if row.get("trial") == trial]

    def compare(self, run_ids: list[str], arm_a: str, arm_b: str, view: str) -> dict:
        rows = [row for run_id in run_ids for row in self._load(run_id)["rows"]]
        table = task_arm_table(rows, view)
        tasks = sorted({task for task, arm in table if arm in {arm_a, arm_b}})
        return {
            "tasks": [
                {
                    "task": task,
                    "arm_a": list(table[(task, arm_a)]) if (task, arm_a) in table else None,
                    "arm_b": list(table[(task, arm_b)]) if (task, arm_b) in table else None,
                }
                for task in tasks
            ],
            "paired_difference": asdict(paired_difference(rows, view, arm_a, arm_b)),
        }

    def over_time(self, run_id: str) -> dict:
        rows = self._load(run_id)["rows"]
        return {
            "all": [asdict(bucket) for bucket in pass_rate_over_time(rows)],
            "by_arm": {
                arm: [asdict(bucket) for bucket in buckets]
                for arm, buckets in pass_rate_over_time_by_arm(rows).items()
            },
        }
