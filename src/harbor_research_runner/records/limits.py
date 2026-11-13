import json
from dataclasses import dataclass, field, fields
from typing import Any, cast


@dataclass(frozen=True)
class Limits:
    tolerated_errors: int = 1
    tolerated_error_fraction: float = 0.02
    trial_output_rate_min: float = 2.0
    first_chunk_max_s: float = 180
    inter_chunk_gap_max_s: float = 180
    request_latency_max_s: float = 1200
    halt_after_consecutive_invalid: int = 5
    zero_billed_window_s: float = 900
    min_spare_memory_mb: int = 2048
    memory_reserve_mb: int = 4096
    max_swapped_out_pages: int = 0
    min_free_disk_gb: int = 20
    host_breach_halt_s: float = 120
    launch_min_interval_s: float = 0
    max_set_asides_per_slot: int = field(kw_only=True)
    verifier_timeout_multiplier: float = field(kw_only=True)
    inactivity_margin_s: float = 600

    @classmethod
    def from_dict(
        cls, overrides: dict, repetitions: int, verifier_timeout_multiplier: float
    ) -> "Limits":
        names = {item.name for item in fields(cls)}
        if "verifier_timeout_multiplier" in overrides:
            raise ValueError("verifier_timeout_multiplier cannot be overridden")
        unknown = set(overrides) - names
        if unknown:
            raise ValueError(f"unknown limit: {sorted(unknown)[0]}")
        values = cast(
            Any,
            {
                "max_set_asides_per_slot": 2 * repetitions,
                "verifier_timeout_multiplier": verifier_timeout_multiplier,
                **overrides,
            },
        )
        return cls(**values)

    @classmethod
    def from_row(cls, row: dict) -> "Limits":
        names = {item.name for item in fields(cls)}
        row = {key: value for key, value in row.items() if key != "max_swap_used_mb"}
        if set(row) != names:
            raise ValueError("limits fields do not match")
        return cls(**row)

    def to_row(self) -> dict:
        return {item.name: getattr(self, item.name) for item in fields(self)}

    def to_json(self) -> str:
        return json.dumps(self.to_row(), sort_keys=True, separators=(",", ":"))


def host_breach(sample: dict, limits: Limits) -> bool:
    return (
        sample["mem_available_mb"] < limits.min_spare_memory_mb
        or sample.get("swapped_out_pages", 0) > limits.max_swapped_out_pages
        or sample["disk_free_gb"] < limits.min_free_disk_gb
        or not sample["docker_ok"]
    )
