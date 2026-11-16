from pathlib import Path

from harbor_research_runner.harbor_adapter.folders import trial_folders


def test_trial_folders_are_sorted_and_require_config(tmp_path: Path) -> None:
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")
    (tmp_path / "z-trial").mkdir()
    (tmp_path / "a-trial").mkdir()
    (tmp_path / "a-trial" / "config.json").write_text("{}", encoding="utf-8")
    (tmp_path / "z-trial" / "config.json").write_text("{}", encoding="utf-8")
    (tmp_path / "not-a-trial").mkdir()

    assert trial_folders(tmp_path) == [tmp_path / "a-trial", tmp_path / "z-trial"]


def test_trial_folders_of_empty_job_are_empty() -> None:
    assert trial_folders(Path("/nonexistent-hrr-job-folder")) == []
