import json
from dataclasses import dataclass, fields


@dataclass(frozen=True)
class Sample:
    t: float
    mem_available_mb: int
    swapped_out_pages: int
    disk_free_gb: float
    load1: float
    trial_containers: int
    docker_ok: bool
    type: str = "sample"

    def to_row(self) -> dict:
        return {item.name: getattr(self, item.name) for item in fields(self)}

    def to_json(self) -> str:
        return json.dumps(self.to_row(), sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_row(cls, row: dict) -> "Sample":
        row = {key: value for key, value in row.items() if key != "swap_used_mb"}
        row = {**row, "swapped_out_pages": row.get("swapped_out_pages", 0)}
        if set(row) != {item.name for item in fields(cls)}:
            raise ValueError("resource sample fields do not match")
        return cls(**row)
