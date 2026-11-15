from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from harbor_research_runner.records.jsonl import read_rows

pytestmark = pytest.mark.timeout(300)


@pytest.mark.parametrize(
    "e2e_run",
    [{"repetitions": 5, "fake": {"sleep_s": 25}}],
    indirect=True,
)
def test_deadline_halts_after_active_verifier_and_resume_completes_slots(e2e_run) -> None:
    stop_launching_at = (datetime.now(UTC) + timedelta(seconds=120)).isoformat()
    started = datetime.now(UTC).timestamp()

    e2e_run.start(stop_launching_at=stop_launching_at)
    halted = e2e_run.wait(timeout=240)

    assert halted.returncode == 2, halted.stdout
    events = e2e_run.events
    deadline_halts = [
        row for row in events if row.get("type") == "halt" and row.get("reason") == "deadline"
    ]
    assert deadline_halts
    deadline_events = [row for row in events if row.get("type") == "stop_launching_at"]
    assert deadline_events
    deadline = datetime.fromisoformat(stop_launching_at).timestamp()
    main_trials = {
        path.name for path in (e2e_run.run_dir / "harbor-job").iterdir() if path.is_dir()
    }
    opened = [
        row
        for row in e2e_run.ledger
        if row.get("type") == "open" and row.get("trial") in main_trials
    ]
    assert any(row["t"] < deadline for row in opened)
    assert all(row["t"] <= deadline for row in opened)
    assert len(opened) < 5
    verdicts = {row["trial"]: row for row in events if row.get("type") == "trial_verdict"}
    before_deadline = [row for row in opened if row["t"] < deadline]
    finished_before_deadline = [
        row for row in before_deadline if verdicts.get(row["trial"], {}).get("valid") is True
    ]
    assert finished_before_deadline
    assert all(
        row.get("trial") != trial["trial"]
        for row in events
        if row.get("type") == "set_aside"
        for trial in finished_before_deadline
    )
    opened_at = {row["trial"]: row["t"] for row in e2e_run.ledger if row.get("type") == "open"}
    assert all(
        opened_at.get(row.get("trial"), 0) <= deadline
        for row in e2e_run.ledger
        if row.get("type") == "request_start"
    )

    resumed = e2e_run.resume()

    assert resumed.returncode == 0, resumed.stdout
    rows = read_rows(e2e_run.evidence_dir / e2e_run.run_id / "trials.jsonl").rows
    final = [row for row in rows if row["location"] == "harbor-job" and row["is_final"]]
    assert len(final) == 5
    assert len({row["trial"] for row in rows}) == len(rows)
    assert any(row.get("type") == "resume" for row in e2e_run.events)
    assert started < deadline
