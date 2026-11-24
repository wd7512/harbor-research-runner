import json
import math
from dataclasses import dataclass, field, fields
from typing import Any, cast

_RETIRED_FIELDS = {"max_swap_used_mb", "zero_billed_window_s", "min_spare_memory_mb"}
_ADDED_FIELDS = {
    "max_swapped_out_pages": 0,
    "memory_reserve_mb": 4096,
    "halt_after_consecutive_failed_requests": 10,
    "host_reserve_mb": 12288,
    "memory_warn_multiplier": 1.0,
    "memory_kill_multiplier": 1.0,
}


@dataclass(frozen=True)
class Limits:
    tolerated_errors: int = 1
    tolerated_error_fraction: float = 0.02
    trial_output_rate_min: float = 2.0
    first_chunk_max_s: float = 180
    inter_chunk_gap_max_s: float = 180
    request_latency_max_s: float = 1200
    halt_after_consecutive_invalid: int = 5
    halt_after_consecutive_failed_requests: int = 10
    memory_reserve_mb: int = 4096
    host_reserve_mb: int = 12288
    max_swapped_out_pages: int = 0
    min_free_disk_gb: int = 20
    host_breach_halt_s: float = 120
    launch_min_interval_s: float = 0
    max_set_asides_per_slot: int = field(kw_only=True)
    verifier_timeout_multiplier: float = field(kw_only=True)
    inactivity_margin_s: float = 600
    memory_warn_multiplier: float = 1.0
    memory_kill_multiplier: float = 1.0

    def __post_init__(self) -> None:
        for name in ("memory_warn_multiplier", "memory_kill_multiplier"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{name} must be a finite positive number")
            if not math.isfinite(value):
                raise ValueError(f"{name} must be finite")
            if value <= 0:
                raise ValueError(f"{name} must be positive")
        if self.memory_warn_multiplier > self.memory_kill_multiplier:
            raise ValueError(
                "memory_warn_multiplier must be less than or equal to memory_kill_multiplier"
            )

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
        row = {key: value for key, value in row.items() if key not in _RETIRED_FIELDS}
        row = {**_ADDED_FIELDS, **row}
        if set(row) != names:
            raise ValueError("limits fields do not match")
        return cls(**cast(Any, row))

    def to_row(self) -> dict:
        return {item.name: getattr(self, item.name) for item in fields(self)}

    def to_json(self) -> str:
        return json.dumps(self.to_row(), sort_keys=True, separators=(",", ":"))


def host_breach(sample: dict, limits: Limits) -> bool:
    return (
        sample.get("swapped_out_pages", 0) > limits.max_swapped_out_pages
        or sample["disk_free_gb"] < limits.min_free_disk_gb
        or not sample["docker_ok"]
    )


def effective_memory_mb(memory_mb: int | None, multiplier: float) -> int | None:
    if memory_mb is None:
        return None
    return math.ceil(memory_mb * multiplier)
