import csv
import json
from functools import lru_cache
from pathlib import Path

from harbor_research_runner.records.trials import TrialRow

_UPSTREAM_STATUSES = {
    "timeout": "stalled",
    "upstream": "server_error",
    "truncated_stream": "truncated",
    "transport": "transport_error",
}


@lru_cache(maxsize=4)
def _proxy_request_data(evidence_dir: Path) -> dict:
    source = evidence_dir / "sources" / "proxy-requests.json"
    return json.loads(source.read_text(encoding="utf-8"))


def old_null_end_count(evidence_dir: Path) -> int:
    return sum(row["t_end"] is None for row in _proxy_request_data(evidence_dir)["rows"])


def old_request_rows(evidence_dir: Path) -> dict[str, list[dict]]:
    """Map OLD requests; null end times use zero duration without estimating one."""
    grouped: dict[str, list[tuple[float, int, dict]]] = {}
    for old_row in _proxy_request_data(evidence_dir)["rows"]:
        trial = old_row["trial_id"].removesuffix("__agent")
        error_category = old_row["error_category"]
        status = (
            "ok"
            if error_category is None and old_row["status_code"] == 200
            else _UPSTREAM_STATUSES[error_category]
        )
        seq = int(old_row["seq"].split("-", 1)[0])
        t_start = old_row["t_start"]
        t_end = old_row["t_end"]
        row = {
            "type": "request_end",
            "seq": seq,
            "t": t_end if t_end is not None else t_start,
            "trial": trial,
            "request_id": old_row["seq"],
            "http_status": old_row["status_code"],
            "upstream_status": status,
            "upstream_http_status": old_row["status_code"],
            "served_model": old_row["model"],
            "prompt_tokens": old_row["prompt_tokens"],
            "completion_tokens": old_row["completion_tokens"],
            "cached_read_tokens": old_row["cached_read_tokens"],
            "completion_chars": 0,
            "first_chunk_s": (
                old_row["t_first_token"] - t_start if old_row["t_first_token"] is not None else None
            ),
            "first_content_s": None,
            "duration_s": t_end - t_start if t_end is not None else 0.0,
            "failovers": 0,
            "error_excerpt": None,
        }
        grouped.setdefault(trial, []).append((t_start, seq, row))
    return {
        trial: [row for _, _, row in sorted(rows, key=lambda item: (item[0], item[1]))]
        for trial, rows in grouped.items()
    }


def old_trial_rows(evidence_dir: Path) -> list[TrialRow]:
    rows = []
    with (evidence_dir / "cells.csv").open(encoding="utf-8", newline="") as source:
        for old_row in csv.DictReader(source):
            reward = float(old_row["reward"]) if old_row["reward"] else None
            row = {
                "trial": old_row["trial"],
                "task": old_row["task"],
                "arm": old_row["version"],
                "harness": None,
                "commit": None,
                "model": None,
                "location": "harbor-job",
                "is_first_attempt": old_row["is_rerun"] == "false",
                "is_final": True,
                "reward": reward,
                "is_pass": reward == 1.0,
                "harbor_exception": None,
                "agent_exit_nonzero": False,
                "environment_setup_started_t": None,
                "environment_setup_finished_t": None,
                "agent_setup_started_t": None,
                "agent_setup_finished_t": None,
                "agent_started_t": None,
                "agent_finished_t": None,
                "verifier_started_t": None,
                "verifier_finished_t": None,
                "valid": True,
                "reason": None,
                "instrument": None,
                "labels": [],
                "measured": False,
                "unmeasured_reason": None,
                "requests": 0,
                "error_requests": 0,
                "refused_requests": 0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "cached_read_tokens": 0,
                "prompt_chars": 0,
                "completion_chars": 0,
                "cost_usd_estimate": 0.0,
                "harbor_reported_tokens": 0,
                "turns": None,
                "tool_calls": None,
                "median_first_content_s": None,
                "output_rate_tokens_per_s": None,
                "agent_wall_s": None,
                "type": "trial",
            }
            rows.append(TrialRow.from_row(row))
    return rows
