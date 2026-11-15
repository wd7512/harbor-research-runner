from __future__ import annotations

import asyncio
import json
import logging
import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest
from harbor.job import Job
from harbor.models.job.config import JobConfig
from harbor.viewer.scanner import JobScanner

from harbor_research_runner.records.jsonl import read_rows

pytestmark = pytest.mark.contract
logger = logging.getLogger(__name__)


def _trial_dirs(job_dir: Path) -> list[Path]:
    return sorted(
        path for path in job_dir.iterdir() if path.is_dir() and not path.name.startswith(".")
    )


def _trial_result(trial_dir: Path) -> dict:
    return json.loads((trial_dir / "result.json").read_text(encoding="utf-8"))


def _timestamp(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def test_resume_with_neutral_filter_deletes_no_result_bearing_trial(harbor_job):
    trial_dir = _trial_dirs(harbor_job.job_dir)[0]
    result_path = trial_dir / "result.json"
    result = json.loads(result_path.read_text(encoding="utf-8"))
    result["exception_info"] = {
        "exception_type": "CancelledError",
        "exception_message": "cancelled for Harbor resume contract",
        "exception_traceback": "CancelledError",
        "occurred_at": datetime.now(UTC).isoformat(),
    }
    result_path.write_text(json.dumps(result), encoding="utf-8")

    neutral = harbor_job.resume()

    assert neutral.returncode == 0, neutral.stdout
    assert trial_dir.is_dir()

    default = harbor_job._run(
        ["uv", "run", "harbor", "jobs", "resume", "-p", str(harbor_job.job_dir)]
    )

    assert default.returncode == 0, default.stdout
    assert not trial_dir.exists()
    assert f"Removing trial directory with CancelledError: {trial_dir.name}" in default.stdout
    logger.info("Default-filter resume output: %s", default.stdout.strip())


def test_resume_deletes_result_less_trial_folder_left_in_job(harbor_job):
    unfinished = harbor_job.job_dir / "unfinished-trial"
    unfinished.mkdir()

    result = harbor_job.resume()

    assert result.returncode == 0, result.stdout
    assert not unfinished.exists()


def test_resume_reruns_slot_whose_folder_was_moved_away(harbor_job):
    trial_dir = _trial_dirs(harbor_job.job_dir)[0]
    set_aside = harbor_job.run_dir / "set-aside-trials"
    set_aside.mkdir()
    shutil.move(str(trial_dir), str(set_aside / trial_dir.name))

    result = harbor_job.resume()

    assert result.returncode == 0, result.stdout
    assert len(_trial_dirs(harbor_job.job_dir)) == 1


def test_rerun_slot_gets_new_trial_name(harbor_job):
    original = _trial_dirs(harbor_job.job_dir)[0]
    set_aside = harbor_job.run_dir / "set-aside-trials"
    set_aside.mkdir()
    shutil.move(str(original), str(set_aside / original.name))

    result = harbor_job.resume()

    assert result.returncode == 0, result.stdout
    rerun = _trial_dirs(harbor_job.job_dir)[0]
    assert rerun.name != original.name


def test_resume_refuses_trial_folder_matching_no_planned_config(harbor_job):
    trial_dir = _trial_dirs(harbor_job.job_dir)[0]
    config_path = trial_dir / "config.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["agent"]["kwargs"]["fake"]["requests"] = 2
    config_path.write_text(json.dumps(config), encoding="utf-8")

    result = harbor_job.resume()

    assert result.returncode != 0
    assert "Existing trial config does not match planned job config" in result.stdout


def test_resume_refuses_changed_job_config(harbor_job):
    config = JobConfig.model_validate_json((harbor_job.job_dir / "config.json").read_text())
    changed = config.model_copy(update={"n_concurrent_trials": config.n_concurrent_trials + 1})

    with pytest.raises(FileExistsError, match="different config"):
        asyncio.run(Job.create(changed))


@pytest.mark.parametrize("harbor_job", [{"fake": {"exit_code": 1}}], indirect=True)
def test_failed_phase_still_records_finish_time(harbor_job):
    result = _trial_result(_trial_dirs(harbor_job.job_dir)[0])

    assert result["exception_info"]["exception_type"] == "NonZeroAgentExitCodeError"
    assert result["agent_execution"]["finished_at"] is not None


@pytest.mark.parametrize("harbor_job", [{"fake": {"sleep_s": 121}}], indirect=True)
def test_agent_timeout_records_exception_and_still_runs_verifier(harbor_job):
    result = _trial_result(_trial_dirs(harbor_job.job_dir)[0])

    assert result["exception_info"]["exception_type"] == "AgentTimeoutError"
    assert result["verifier_result"] is not None
    assert result["verifier_result"]["rewards"]


def test_agent_end_event_fires_before_verifier_starts(harbor_job):
    trial_dir = _trial_dirs(harbor_job.job_dir)[0]
    result = _trial_result(trial_dir)
    rows = read_rows(harbor_job.ledger_path).rows
    close = next(row for row in rows if row.get("type") == "close")

    assert close["trial"] == trial_dir.name
    assert close["t"] < _timestamp(result["verifier"]["started_at"])


def test_harness_install_runs_in_agent_setup_phase(harbor_job):
    trial_dir = _trial_dirs(harbor_job.job_dir)[0]
    result = _trial_result(trial_dir)
    rows = read_rows(harbor_job.ledger_path).rows
    opened = next(row for row in rows if row.get("type") == "open")

    assert opened["trial"] == trial_dir.name
    assert opened["t"] > _timestamp(result["agent_setup"]["finished_at"])


def test_session_id_is_trial_name_plus_agent_suffix(harbor_job):
    trial_dir = _trial_dirs(harbor_job.job_dir)[0]
    opened = next(
        row for row in read_rows(harbor_job.ledger_path).rows if row.get("type") == "open"
    )

    assert opened["trial"] == trial_dir.name


def test_harbor_contract_accepts_proxy_refusal_body(harbor_job):
    refusal_run = harbor_job.run_refused()
    trial_dir = _trial_dirs(harbor_job.refusal_run_dir / "harbor-job")[0]
    result = _trial_result(trial_dir)
    rows = read_rows(harbor_job.ledger_path).rows
    refused = next(row for row in rows if row.get("type") == "refused")

    assert refusal_run.returncode == 0, refusal_run.stdout
    assert refused["reason"] == "not_open"
    assert result["exception_info"]["exception_type"] == "NonZeroAgentExitCodeError"
    assert not result["exception_info"]["exception_type"].startswith("Api")


@pytest.mark.parametrize("harbor_job", [{"repetitions": 2}], indirect=True)
def test_harbor_viewer_scanner_reads_job_and_set_aside_folders(harbor_job):
    trial_dir = _trial_dirs(harbor_job.job_dir)[0]
    set_aside = harbor_job.run_dir / "set-aside-trials"
    set_aside.mkdir()
    shutil.move(str(trial_dir), str(set_aside / trial_dir.name))
    scanner = JobScanner(harbor_job.run_dir)

    assert {"harbor-job", "set-aside-trials"} <= set(scanner.list_jobs())
    assert scanner.list_trials("harbor-job") == [
        path.name for path in _trial_dirs(harbor_job.job_dir)
    ]
    assert scanner.list_trials("set-aside-trials") == [trial_dir.name]
