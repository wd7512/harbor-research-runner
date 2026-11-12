from __future__ import annotations

import json
import tomllib
from pathlib import Path

from harbor.constants import CACHE_DIR
from harbor.models.trial.config import TaskConfig

from harbor_research_runner.harbor_adapter.job_folder import HarborFolderError, trial_folders


def task_tomls(job_dir: Path, cache_root: Path | None = None) -> dict[str, str]:
    texts: dict[str, str] = {}
    for trial_dir in trial_folders(job_dir):
        config_path = trial_dir / "config.json"
        task_data = {}
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
            task_data = config["task"]
            task_config = TaskConfig.model_validate(task_data)
            task_id = task_config.get_task_id()
            task_name = task_id.get_name()
            task_dir = task_config.get_local_path()
            if cache_root is not None:
                task_dir = cache_root / task_dir.relative_to(CACHE_DIR)
        except Exception as exc:
            task_hint = task_data.get("path") or task_data.get("name") or "<unresolved task>"
            path_tried = Path(str(task_hint)) / "task.toml"
            raise HarborFolderError(
                f"{trial_dir.name}: cannot resolve task.toml path {path_tried}"
            ) from exc

        task_path = task_dir / "task.toml"
        try:
            text = task_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise HarborFolderError(
                f"{trial_dir.name}: cannot read task.toml at {task_path}"
            ) from exc
        if task_config.name is None:
            try:
                task_toml = tomllib.loads(text)
            except tomllib.TOMLDecodeError as exc:
                raise HarborFolderError(
                    f"{trial_dir.name}: invalid task.toml at {task_path}"
                ) from exc
            task_metadata = task_toml.get("task")
            task_metadata = task_metadata if isinstance(task_metadata, dict) else {}
            task_name = task_metadata.get("name")
            if not isinstance(task_name, str):
                raise HarborFolderError(f"{trial_dir.name}: missing task name in {task_path}")
        previous = texts.get(task_name)
        if previous is not None and previous != text:
            raise HarborFolderError(
                f"{trial_dir.name}: conflicting task.toml contents at {task_path}"
            )
        texts[task_name] = text
    return texts
