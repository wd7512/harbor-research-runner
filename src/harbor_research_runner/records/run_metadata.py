import json
import os
import tempfile
from dataclasses import dataclass, fields
from pathlib import Path


@dataclass(frozen=True)
class RunMetadata:
    run_id: str
    created_t: float
    tool_version: str
    tool_commit: str
    harbor_version: str
    job: dict
    job_file_sha256: str
    model_card: dict
    arms: list[dict]
    task_set: dict
    machine_check_sha256: str
    host: dict
    proxy: dict
    limits: dict

    def to_row(self) -> dict:
        return {item.name: getattr(self, item.name) for item in fields(self)}

    def to_json(self) -> str:
        return json.dumps(self.to_row(), sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_row(cls, row: dict) -> "RunMetadata":
        if set(row) != {item.name for item in fields(cls)}:
            raise ValueError("run metadata fields do not match")
        return cls(**row)


def write_once(path: Path, metadata: RunMetadata) -> None:
    descriptor, temporary_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(metadata.to_json())
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def read(path: Path) -> RunMetadata:
    try:
        row = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError("invalid run metadata JSON") from exc
    if not isinstance(row, dict):
        raise ValueError("run metadata must be a JSON object")
    return RunMetadata.from_row(row)
