from __future__ import annotations

import json
from pathlib import Path

import pytest
from harbor.models.job.config import DatasetConfig

from harbor_research_runner.harbor_adapter.job_folder import (
    HarborFolderError,
    read_trial,
    task_budgets,
    task_digests,
)
from harbor_research_runner.harbor_adapter.task_files import task_id, task_tomls

FIXTURE_ROOT = Path(__file__).parents[2] / "fixtures" / "harbor_job"
TASK = "terminal-bench/bn-fit-modify"
TASK_TOML = (FIXTURE_ROOT / "task-tomls" / "bn-fit-modify.task.toml").read_text(encoding="utf-8")


def _trial_copy(tmp_path: Path) -> Path:
    job_dir = tmp_path / "harbor-job"
    trial_dir = job_dir / "pass-bn-fit"
    trial_dir.mkdir(parents=True)
    config = json.loads((FIXTURE_ROOT / "pass-bn-fit" / "config.json").read_text())
    (trial_dir / "config.json").write_text(json.dumps(config), encoding="utf-8")
    (trial_dir / "lock.json").write_bytes((FIXTURE_ROOT / "pass-bn-fit" / "lock.json").read_bytes())
    return job_dir


def _cached_task(cache_root: Path) -> Path:
    relative_cache = Path("tasks/packages") / "terminal-bench" / "bn-fit-modify"
    fixture_ref = "b5f9644970c17ad9ddb46b7266f7bcd87c761d77d7e6f55d7cfe7284d5ff66e9"
    task_dir = cache_root / relative_cache / fixture_ref
    task_dir.mkdir(parents=True)
    (task_dir / "task.toml").write_text(TASK_TOML, encoding="utf-8")
    return task_dir


def test_task_tomls_reads_package_task_and_feeds_task_budget(tmp_path: Path) -> None:
    job_dir = _trial_copy(tmp_path)
    _cached_task(tmp_path)

    task_texts = task_tomls(job_dir, cache_root=tmp_path)

    assert task_texts == {TASK: TASK_TOML}
    assert task_budgets(job_dir, task_texts) == {TASK: (3600.0, 3600.0)}


def test_task_tomls_names_trial_and_path_when_task_file_is_missing(tmp_path: Path) -> None:
    job_dir = _trial_copy(tmp_path)
    task_dir = _cached_task(tmp_path)
    (task_dir / "task.toml").unlink()

    with pytest.raises(HarborFolderError, match=r"pass-bn-fit.*task.toml"):
        task_tomls(job_dir, cache_root=tmp_path)


def test_local_task_toml_uses_task_id_instead_of_metadata_name(tmp_path: Path) -> None:
    job_dir = _trial_copy(tmp_path)
    task_dir = tmp_path / "say-hello"
    task_dir.mkdir()
    task_text = '[task]\nname = "canary/say-hello"\n'
    (task_dir / "task.toml").write_text(task_text, encoding="utf-8")
    trial_dir = job_dir / "pass-bn-fit"
    config = json.loads((trial_dir / "config.json").read_text(encoding="utf-8"))
    config["task"] = {"path": str(task_dir)}
    (trial_dir / "config.json").write_text(json.dumps(config), encoding="utf-8")

    assert task_tomls(job_dir) == {"say-hello": task_text}


def test_local_task_name_matches_harbor_filter_and_trial_records(tmp_path: Path) -> None:
    job_dir = _trial_copy(tmp_path)
    task_dir = tmp_path / "say-hello"
    task_dir.mkdir()
    task_text = (
        '[task]\nname = "org/say-hello"\n[agent]\ntimeout_sec = 12\n[verifier]\ntimeout_sec = 3\n'
    )
    (task_dir / "task.toml").write_text(task_text, encoding="utf-8")
    trial_dir = job_dir / "pass-bn-fit"
    config = json.loads((trial_dir / "config.json").read_text(encoding="utf-8"))
    config["task"] = {"path": str(task_dir)}
    (trial_dir / "config.json").write_text(json.dumps(config), encoding="utf-8")
    result = json.loads((FIXTURE_ROOT / "pass-bn-fit" / "result.json").read_text())
    result["task_name"] = "org/say-hello"
    (trial_dir / "result.json").write_text(json.dumps(result), encoding="utf-8")
    lock = json.loads((FIXTURE_ROOT / "pass-bn-fit" / "lock.json").read_text())
    lock["task"] = {"name": "say-hello", "digest": "sha256:" + "a" * 64}
    (trial_dir / "lock.json").write_text(json.dumps(lock), encoding="utf-8")

    harbor_task_id = task_id(config["task"])
    runner_name = harbor_task_id.get_name()
    selected = DatasetConfig(path=tmp_path, task_names=[runner_name])._filter_task_ids(
        [harbor_task_id]
    )

    assert [item.get_name() for item in selected] == ["say-hello"]
    assert read_trial(trial_dir).task == "say-hello"
    assert task_tomls(job_dir) == {"say-hello": task_text}
    assert task_budgets(job_dir, {"say-hello": task_text}) == {"say-hello": (12.0, 3.0)}
    assert task_digests(job_dir) == {"say-hello": "sha256:" + "a" * 64}


def test_package_task_name_remains_the_recorded_name() -> None:
    trial_dir = FIXTURE_ROOT / "fail-break-filter"
    config = json.loads((trial_dir / "config.json").read_text(encoding="utf-8"))
    result = json.loads((trial_dir / "result.json").read_text(encoding="utf-8"))

    assert config["task"]["name"] == result["task_name"]
    assert read_trial(trial_dir).task == "terminal-bench/break-filter-js-from-html"
