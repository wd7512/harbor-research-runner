from __future__ import annotations

import json
import os
import re
import subprocess
import tomllib
from pathlib import Path

import pytest

_BUDGET_BREACH = re.compile(r"of (?:cpu|wall) time, over the \d+(?:\.\d+)?s budget")


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    if report.failed and _BUDGET_BREACH.search(str(report.longrepr)):
        mutant = os.environ.get("MUTANT_UNDER_TEST")
        if mutant:
            marker_dir = Path.cwd().parent / ".agent" / "mutation-timeouts"
            marker_dir.mkdir(parents=True, exist_ok=True)
            (marker_dir / f"{os.getpid()}.json").write_text(json.dumps(mutant), encoding="utf-8")


def main() -> int:
    root = Path.cwd()
    marker_dir = root / ".agent" / "mutation-timeouts"
    marker_dir.mkdir(parents=True, exist_ok=True)
    for marker in marker_dir.glob("*.json"):
        marker.unlink()

    environment = os.environ.copy()
    environment["UV_PROJECT_ENVIRONMENT"] = str(root / ".venv")
    sync = subprocess.run(
        ["uv", "sync", "--all-extras", "--group", "mutation", "--locked"],
        check=False,
        env=environment,
    )
    if sync.returncode != 0:
        return sync.returncode

    environment["UV_NO_SYNC"] = "1"
    mutated_sources = str(root / "mutants" / "src")
    existing_pythonpath = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = os.pathsep.join(
        [mutated_sources, *([existing_pythonpath] if existing_pythonpath else [])]
    )
    process = subprocess.run(["uv", "run", "mutmut", "run"], check=False, env=environment)
    if process.returncode != 0:
        return process.returncode

    timed_out_mutants = {
        json.loads(marker.read_text(encoding="utf-8")) for marker in marker_dir.glob("*.json")
    }
    results: list[dict[str, str | int]] = []
    config = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["mutmut"]
    for source_path in config["only_mutate"]:
        metadata_path = root / "mutants" / f"{source_path}.meta"
        if not metadata_path.exists():
            continue
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        for mutant, exit_code in sorted(metadata["exit_code_by_key"].items()):
            if mutant in timed_out_mutants or exit_code not in (0, 1):
                status = "unverified"
            elif exit_code == 0:
                status = "survived"
            else:
                status = "killed"
            results.append({"mutant": mutant, "status": status, "exit_code": exit_code})

    results_path = root / ".agent" / "mutation-results.json"
    results_path.write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
    counts = {
        status: sum(result["status"] == status for result in results)
        for status in ("killed", "survived", "unverified")
    }
    print(json.dumps({"results": str(results_path), "counts": counts}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
