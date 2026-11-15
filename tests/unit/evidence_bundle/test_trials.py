import json
import shutil
from pathlib import Path

from harbor_research_runner.evidence_bundle.trials import build_trial_rows
from harbor_research_runner.run.canary import canary_memory_mb

FIXTURES = Path(__file__).parents[2] / "fixtures" / "run_folders"


def _expected(folder: Path) -> list[dict]:
    rows = [
        json.loads(line) for line in (folder / "expected-trials.jsonl").read_text().splitlines()
    ]
    machine_check = json.loads((folder / "machine-check.json").read_text(encoding="utf-8"))
    task_memory = {row["name"]: row.get("memory_mb") for row in machine_check["tasks"]}
    for row in rows:
        row["oom_source"] = None
        row["peak_memory_mb"] = None
        row["memory_limit_mb"] = task_memory.get(row["task"])
    return rows


def test_clean_run_trial_rows_match_expected() -> None:
    folder = FIXTURES / "clean"
    assert [row.to_row() for row in build_trial_rows(folder)] == _expected(folder)


def test_set_aside_and_unmeasured_trial_rows_match_expected() -> None:
    folder = FIXTURES / "retries"
    assert [row.to_row() for row in build_trial_rows(folder)] == _expected(folder)


def test_every_slot_has_k_first_attempts() -> None:
    rows = build_trial_rows(FIXTURES / "clean")
    slots = {(row.task, row.arm) for row in rows if row.location != "canary-job"}
    assert all(
        sum(row.is_first_attempt for row in rows if (row.task, row.arm) == slot) == 1
        for slot in slots
    )


def test_trial_without_verdict_is_absent() -> None:
    rows = build_trial_rows(FIXTURES / "retries")
    assert all(row.trial != "unfinished" for row in rows)


def test_canary_is_flagged_and_not_a_first_attempt() -> None:
    rows = build_trial_rows(FIXTURES / "clean")
    canaries = [row for row in rows if row.location == "canary-job"]
    assert canaries
    assert all(not row.is_first_attempt and not row.is_final for row in canaries)


def test_reading_same_run_twice_gives_identical_rows() -> None:
    folder = FIXTURES / "clean"
    assert build_trial_rows(folder) == build_trial_rows(folder)


def test_trial_rows_include_memory_peak_and_task_limit(tmp_path: Path) -> None:
    folder = tmp_path / "clean"
    shutil.copytree(FIXTURES / "clean", folder)
    initial_rows = build_trial_rows(folder)
    trial = next(row for row in initial_rows if row.location == "harbor-job")
    machine_check_path = folder / "machine-check.json"
    machine_check = json.loads(machine_check_path.read_text(encoding="utf-8"))
    task = next(row for row in machine_check["tasks"] if row["name"] == trial.task)
    task["memory_mb"] = 128
    machine_check_path.write_text(json.dumps(machine_check), encoding="utf-8")
    (folder / "memory-watch.jsonl").write_text(
        json.dumps(
            {
                "type": "sample",
                "t": 10.0,
                "trial": trial.trial,
                "location": "harbor-job",
                "container": "container-a",
                "used_mb": 42.5,
                "limit_mb": 128,
                "reader": "docker_stats",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    measured = next(row for row in build_trial_rows(folder) if row.trial == trial.trial)

    assert measured.peak_memory_mb == 42.5
    assert measured.memory_limit_mb == 128
    assert measured.oom_source is None


def test_canary_trial_evidence_uses_its_recorded_memory_limit(tmp_path: Path) -> None:
    folder = tmp_path / "clean"
    shutil.copytree(FIXTURES / "clean", folder)
    canary = next(row for row in build_trial_rows(folder) if row.location == "canary-job")
    (folder / "memory-watch.jsonl").write_text(
        json.dumps(
            {
                "type": "sample",
                "t": 10.0,
                "trial": canary.trial,
                "location": "canary-job",
                "container": "container-a",
                "used_mb": 16,
                "limit_mb": canary_memory_mb(),
                "reader": "docker_stats",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    measured = next(row for row in build_trial_rows(folder) if row.trial == canary.trial)

    assert measured.peak_memory_mb == 16
    assert measured.memory_limit_mb == canary_memory_mb()
