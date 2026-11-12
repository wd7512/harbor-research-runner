import json
from pathlib import Path

from harbor_research_runner.evidence_bundle.trials import build_trial_rows

FIXTURES = Path(__file__).parents[2] / "fixtures" / "run_folders"


def _expected(folder: Path) -> list[dict]:
    return [
        json.loads(line) for line in (folder / "expected-trials.jsonl").read_text().splitlines()
    ]


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
