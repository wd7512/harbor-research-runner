import json
import os
import tempfile
from dataclasses import dataclass, fields
from pathlib import Path


@dataclass(frozen=True)
class MachineCheck:
    created_t: float
    tool_version: str
    harbor_version: str
    host_fingerprint: str
    task_source: dict
    concurrency: int
    tasks: list[dict]
    included: list[str]
    verifier_timeout_multiplier: float

    def to_row(self) -> dict:
        return {item.name: getattr(self, item.name) for item in fields(self)}

    def to_json(self) -> str:
        return json.dumps(self.to_row(), sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_row(cls, row: dict) -> "MachineCheck":
        if set(row) != {item.name for item in fields(cls)}:
            raise ValueError("machine check fields do not match")
        return cls(**row)


def write(path: Path, check: MachineCheck) -> None:
    descriptor, temporary_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(check.to_json())
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def read(path: Path) -> MachineCheck:
    try:
        row = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError("invalid machine check JSON") from exc
    if not isinstance(row, dict):
        raise ValueError("machine check must be a JSON object")
    return MachineCheck.from_row(row)
