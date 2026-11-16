from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import signal
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from harbor.job import Job
from harbor.models.job.config import DatasetConfig, JobConfig
from harbor.viewer.scanner import JobScanner

from harbor_research_runner.harbor_adapter.commands import job_config as harbor_job_config
from harbor_research_runner.harbor_adapter.commands import run_command
from harbor_research_runner.harbor_adapter.job_folder import read_trial
from harbor_research_runner.harbor_adapter.task_files import task_content_hash
from harbor_research_runner.job_config.job_file import load_job
from harbor_research_runner.records.jsonl import read_rows
from harbor_research_runner.records.machine_check import MachineCheck
from harbor_research_runner.run.checks import resolve_tasks
from harbor_research_runner.trial_validity.phase_table import failed_phase

pytestmark = pytest.mark.contract
logger = logging.getLogger(__name__)
TASK_SOURCE = Path(__file__).parents[2] / "src/harbor_research_runner/run/canary_task/say-hello"


def _trial_dirs(job_dir: Path) -> list[Path]:
    return sorted(
        path for path in job_dir.iterdir() if path.is_dir() and not path.name.startswith(".")
    )


def _trial_result(trial_dir: Path) -> dict:
    return json.loads((trial_dir / "result.json").read_text(encoding="utf-8"))


def _timestamp(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout


def _git_task_source(tmp_path: Path) -> tuple[str, str]:
    repository = tmp_path / "task-repository"
    repository.mkdir()
    shutil.copytree(TASK_SOURCE, repository / "say-hello")
    _git("init", str(repository))
    _git("-C", str(repository), "config", "user.email", "contract@example.invalid")
    _git("-C", str(repository), "config", "user.name", "Harbor Contract")
    _git("-C", str(repository), "add", "say-hello")
    _git("-C", str(repository), "commit", "-m", "Canary task")
    commit = _git("-C", str(repository), "rev-parse", "HEAD").strip()
    bare_repository = tmp_path / "task-repository.git"
    _git("clone", "--bare", str(repository), str(bare_repository))
    return bare_repository.as_uri(), commit


def _machine_check(task_source: dict) -> MachineCheck:
    return MachineCheck(
        created_t=0.0,
        tool_version="test",
        harbor_version="0.23.0",
        host_fingerprint="test",
        task_source=task_source,
        concurrency=1,
        tasks=[{"name": "say-hello"}],
        included=["say-hello"],
        verifier_timeout_multiplier=1.0,
    )


def _git_harbor_config(harbor_job, tmp_path: Path, url: str, commit: str) -> dict:
    existing = yaml.safe_load(
        (harbor_job.run_dir / "harbor-job-config.yaml").read_text(encoding="utf-8")
    )
    agent = existing["agents"][0]
    job_file = tmp_path / "git-job.yaml"
    job_file.write_text(
        yaml.safe_dump(
            {
                "name": "contract-git-job",
                "model": "fake/model",
                "repetitions": 1,
                "concurrency": 1,
                "task_order": "slowest_first",
                "tasks": {
                    "git": {"url": url, "commit": commit},
                    "include": ["say-hello"],
                },
                "machine_check": "checks/unused.json",
                "arms": [{"label": "fake-arm", "harness": "fake", "commit": "0" * 40}],
                "proxy": {
                    "host": "127.0.0.1",
                    "port": 8766,
                    "container_host": "host.docker.internal",
                },
                "limits": {},
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    job = load_job(job_file)
    return harbor_job_config(
        job,
        ["say-hello"],
        [(agent["import_path"], agent["kwargs"])],
        verifier_timeout_multiplier=1.0,
    )


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


def test_empty_task_names_selects_every_local_task(tmp_path: Path):
    tasks = tmp_path / "tasks"
    tasks.mkdir()
    source = Path(__file__).parents[2] / "src/harbor_research_runner/run/canary_task/say-hello"
    for name in ("alpha", "beta"):
        shutil.copytree(source, tasks / name)

    empty_filter = asyncio.run(DatasetConfig(path=tasks, task_names=[]).get_task_configs())
    named_filter = asyncio.run(DatasetConfig(path=tasks, task_names=["alpha"]).get_task_configs())

    assert {task.get_task_id().get_name() for task in empty_filter} == {"alpha", "beta"}
    assert [task.get_task_id().get_name() for task in named_filter] == ["alpha"]


def test_local_trial_path_and_lock_digest_match_harbor(harbor_job):
    trial_dir = _trial_dirs(harbor_job.job_dir)[0]
    config = json.loads((trial_dir / "config.json").read_text(encoding="utf-8"))
    lock = json.loads((trial_dir / "lock.json").read_text(encoding="utf-8"))
    task_path = Path(config["task"]["path"])

    assert task_path.name == "say-hello"
    assert lock["task"]["digest"] == task_content_hash(task_path)


def test_hrr_local_task_filter_matches_harbor_resolution(harbor_job, tmp_path: Path):
    config = yaml.safe_load(
        (harbor_job.run_dir / "harbor-job-config.yaml").read_text(encoding="utf-8")
    )
    dataset = DatasetConfig.model_validate(config["datasets"][0])
    source = Path(dataset.path)
    copied_source = tmp_path / "tasks"
    shutil.copytree(source, copied_source)
    shutil.copytree(source / "say-hello", copied_source / "decoy")
    dataset = dataset.model_copy(update={"path": copied_source})

    resolved = asyncio.run(dataset.get_task_configs())
    job_definition = load_job(harbor_job.run_dir.parent / "job.yaml")
    hrr_tasks = resolve_tasks(job_definition, _machine_check({"path": str(source)}))
    trial_tasks = {
        json.loads((trial / "config.json").read_text(encoding="utf-8"))["task"]["path"]
        for trial in _trial_dirs(harbor_job.job_dir)
    }

    assert set(hrr_tasks) == {task.get_task_id().get_name() for task in resolved} == {"say-hello"}
    assert len(trial_tasks) == 1


@pytest.mark.parametrize("harbor_job", [{"defer_run": True}], indirect=True)
def test_git_task_download_map_and_lock_digest_match_harbor(harbor_job, tmp_path: Path):
    url, commit = _git_task_source(tmp_path)
    config = _git_harbor_config(harbor_job, tmp_path, url, commit)
    config_path = harbor_job.run_dir / "harbor-job-config.yaml"
    config_path.write_text(yaml.safe_dump(config, sort_keys=True), encoding="utf-8")
    harbor_config = JobConfig.model_validate(config)
    resolved_job = asyncio.run(Job.create(harbor_config))
    job_definition = load_job(tmp_path / "git-job.yaml")
    hrr_tasks = resolve_tasks(
        job_definition, _machine_check({"git": {"url": url, "commit": commit}})
    )
    harbor_tasks = [task.get_task_id().get_name() for task in resolved_job._task_configs]
    task_config = harbor_config.tasks[0]
    task_id = task_config.get_task_id()
    downloaded = resolved_job._task_download_results[task_id]

    assert list(resolved_job._task_download_results) == [task_id]
    assert hrr_tasks == harbor_tasks == ["say-hello"]
    assert downloaded.path == task_id.get_local_path()
    assert (downloaded.path / "task.toml").is_file()

    result = harbor_job._run(
        run_command(harbor_job.run_dir, harbor_job.job_name, harbor_job.proxy_url, "slowest_first")
    )
    assert result.returncode == 0, result.stdout
    trial_dir = _trial_dirs(harbor_job.job_dir)[0]
    lock = json.loads((trial_dir / "lock.json").read_text(encoding="utf-8"))

    assert lock["task"]["digest"] == task_content_hash(downloaded.path)


@pytest.mark.parametrize("harbor_job", [{"defer_run": True}], indirect=True)
def test_environment_setup_failure_folder_matches_harbor(harbor_job, tmp_path: Path):
    tasks = tmp_path / "tasks"
    tasks.mkdir()
    failed_task = tasks / "say-hello"
    shutil.copytree(TASK_SOURCE, failed_task)
    (failed_task / "environment" / "Dockerfile").write_text("INVALID\n", encoding="utf-8")
    config_path = harbor_job.run_dir / "harbor-job-config.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config["datasets"][0]["path"] = str(tasks)
    config_path.write_text(yaml.safe_dump(config, sort_keys=True), encoding="utf-8")

    result = harbor_job.run()
    trial_dir = _trial_dirs(harbor_job.job_dir)[0]
    trial_result = _trial_result(trial_dir)
    facts = read_trial(trial_dir)
    trial_files = {
        path.relative_to(trial_dir).as_posix() for path in trial_dir.rglob("*") if path.is_file()
    }

    assert result.returncode == 0, result.stdout
    assert trial_files == {
        "config.json",
        "lock.json",
        "result.json",
        "trial.log",
        "exception.txt",
        "artifacts/manifest.json",
    }
    assert trial_result["exception_info"]["exception_type"] == "RuntimeError"
    assert trial_result["environment_setup"]["started_at"] is not None
    assert trial_result["environment_setup"]["finished_at"] is not None
    assert trial_result["agent_setup"] is None
    assert trial_result["agent_execution"] is None
    assert trial_result["verifier"] is None
    assert failed_phase(facts) == "environment_setup"


@pytest.mark.parametrize(
    "harbor_job", [{"defer_run": True, "fake": {"sleep_s": 120}}], indirect=True
)
def test_interrupted_job_keeps_harbor_trial_folder(harbor_job):
    process = harbor_job.start()
    deadline = time.monotonic() + 60
    opened = None
    while time.monotonic() < deadline:
        rows = read_rows(harbor_job.ledger_path).rows
        opened = next((row for row in rows if row.get("type") == "open"), None)
        if opened is not None:
            break
        if process.poll() is not None:
            output, _ = process.communicate()
            raise AssertionError(f"Harbor exited before opening the trial: {output}")
        time.sleep(0.05)
    assert opened is not None, "Harbor did not open a trial before the deadline"

    os.killpg(process.pid, signal.SIGINT)
    result = harbor_job.wait(timeout=60)
    trial_dir = harbor_job.job_dir / opened["trial"]
    trial_result = _trial_result(trial_dir)
    facts = read_trial(trial_dir)

    assert result.returncode == 128 + signal.SIGINT, result.stdout
    assert trial_dir.is_dir()
    assert {
        path.relative_to(trial_dir).as_posix() for path in trial_dir.rglob("*") if path.is_file()
    } == {
        "artifacts/manifest.json",
        "lock.json",
        "result.json",
        "config.json",
        "agent/harness-facts.json",
        "trial.log",
        "exception.txt",
    }
    assert {path.name for path in harbor_job.job_dir.iterdir() if path != trial_dir} == {
        "job.log",
        "lock.json",
        "result.json",
        "config.json",
    }
    assert trial_result["exception_info"]["exception_type"] == "CancelledError"
    assert trial_result["verifier_result"] is None
    assert facts.exception_type == "CancelledError"
    assert failed_phase(facts) == "agent"


@pytest.mark.parametrize(
    ("harbor_job", "exception"),
    [
        ({"fake": {"exit_code": 1}}, "NonZeroAgentExitCodeError"),
        ({"fake": {"solve": False}}, None),
    ],
    ids=["agent-failure", "verifier-failure"],
    indirect=["harbor_job"],
)
def test_failed_trial_keeps_harbor_phase_result_and_lock(harbor_job, exception):
    trial_dir = _trial_dirs(harbor_job.job_dir)[0]
    result = _trial_result(trial_dir)
    lock = json.loads((trial_dir / "lock.json").read_text(encoding="utf-8"))

    assert (trial_dir / "config.json").is_file()
    assert isinstance(lock["task"]["digest"], str)
    if exception is None:
        assert result["exception_info"] is None
        assert result["verifier_result"]["rewards"] == {"reward": 0.0}
    else:
        assert result["exception_info"]["exception_type"] == exception
