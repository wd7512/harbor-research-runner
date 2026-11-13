from __future__ import annotations

from pathlib import Path

import yaml


def test_every_e2e_test_file_is_assigned_to_exactly_one_ci_shard() -> None:
    root = Path(__file__).parents[2]
    workflow = yaml.safe_load((root / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
    shards = (
        workflow.get("jobs", {})
        .get("e2e", {})
        .get("strategy", {})
        .get("matrix", {})
        .get("shard", [])
    )
    assignments = [file for shard in shards for file in shard.get("files", [])]
    actual = {path.relative_to(root).as_posix() for path in (root / "tests/e2e").glob("test_*.py")}
    assigned = set(assignments)
    duplicates = {file for file in assignments if assignments.count(file) > 1}

    assert actual == assigned and not duplicates, {
        "missing": sorted(actual - assigned),
        "unknown": sorted(assigned - actual),
        "duplicates": sorted(duplicates),
    }
