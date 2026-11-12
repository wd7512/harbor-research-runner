from __future__ import annotations

import json
from pathlib import Path

import pytest

from harbor_research_runner.harbor_adapter.job_folder import HarborFolderError, task_budgets
from harbor_research_runner.harbor_adapter.task_files import task_tomls

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


def test_local_task_toml_uses_the_task_name_from_its_metadata(tmp_path: Path) -> None:
    job_dir = _trial_copy(tmp_path)
    task_dir = tmp_path / "say-hello"
    task_dir.mkdir()
    task_text = '[task]\nname = "canary/say-hello"\n'
    (task_dir / "task.toml").write_text(task_text, encoding="utf-8")
    trial_dir = job_dir / "pass-bn-fit"
    config = json.loads((trial_dir / "config.json").read_text(encoding="utf-8"))
    config["task"] = {"path": str(task_dir)}
    (trial_dir / "config.json").write_text(json.dumps(config), encoding="utf-8")

    assert task_tomls(job_dir) == {"canary/say-hello": task_text}
