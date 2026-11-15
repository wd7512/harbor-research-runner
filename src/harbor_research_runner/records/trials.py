import json
from dataclasses import dataclass, fields


@dataclass(frozen=True)
class TrialRow:
    trial: str
    task: str
    arm: str
    harness: str
    commit: str
    model: str
    location: str
    is_first_attempt: bool
    is_final: bool
    reward: float | None
    is_pass: bool
    harbor_exception: str | None
    agent_exit_nonzero: bool
    environment_setup_started_t: float | None
    environment_setup_finished_t: float | None
    agent_setup_started_t: float | None
    agent_setup_finished_t: float | None
    agent_started_t: float | None
    agent_finished_t: float | None
    verifier_started_t: float | None
    verifier_finished_t: float | None
    valid: bool
    reason: str | None
    instrument: str | None
    labels: list[str]
    measured: bool
    unmeasured_reason: str | None
    oom_source: str | None
    requests: int
    error_requests: int
    refused_requests: int
    prompt_tokens: int
    completion_tokens: int
    cached_read_tokens: int
    prompt_chars: int
    completion_chars: int
    cost_usd_estimate: float
    harbor_reported_tokens: int
    turns: int | None
    tool_calls: int | None
    median_first_content_s: float | None
    output_rate_tokens_per_s: float | None
    agent_wall_s: float | None
    peak_memory_mb: float | None
    memory_limit_mb: int | None
    type: str = "trial"

    def to_row(self) -> dict:
        return {item.name: getattr(self, item.name) for item in fields(self)}

    def to_json(self) -> str:
        return json.dumps(self.to_row(), sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_row(cls, row: dict) -> "TrialRow":
        if set(row) != {item.name for item in fields(cls)}:
            raise ValueError("trial fields do not match")
        return cls(**row)
