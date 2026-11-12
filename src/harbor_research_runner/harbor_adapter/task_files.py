from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from harbor.constants import CACHE_DIR
from harbor.models.task.id import GitTaskId, LocalTaskId, PackageTaskId
from harbor.models.trial.config import TaskConfig

from harbor_research_runner.harbor_adapter.folders import HarborFolderError, trial_folders


def task_id(task_data: Mapping[str, Any]) -> GitTaskId | LocalTaskId | PackageTaskId:
    return TaskConfig.model_validate(task_data).get_task_id()


def task_tomls(job_dir: Path, cache_root: Path | None = None) -> dict[str, str]:
    texts: dict[str, str] = {}
    for trial_dir in trial_folders(job_dir):
        config_path = trial_dir / "config.json"
        task_data = {}
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
            task_data = config["task"]
            task = task_id(task_data)
            task_name = task.get_name()
            task_dir = task.get_local_path()
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
        previous = texts.get(task_name)
        if previous is not None and previous != text:
            raise HarborFolderError(
                f"{trial_dir.name}: conflicting task.toml contents at {task_path}"
            )
        texts[task_name] = text
    return texts
