from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from statistics import median

from harbor_research_runner.harbor_adapter.job_folder import (
    read_trial,
    read_trial_details,
    trial_folders,
)
from harbor_research_runner.records.jsonl import read_rows
from harbor_research_runner.records.proxy_ledger import COUNTS_AGAINST_TRIAL
from harbor_research_runner.records.trials import TrialRow
from harbor_research_runner.statistics.cost import cost_usd


def build_trial_rows(run_dir: Path) -> list[TrialRow]:
    metadata = json.loads((run_dir / "run-metadata.json").read_text(encoding="utf-8"))
    arms = {arm["label"]: arm for arm in metadata["arms"]}
    verdicts = {
        row["trial"]: row
        for row in read_rows(run_dir / "run-events.jsonl").rows
        if row.get("type") == "trial_verdict"
    }
    ledger_rows = read_rows(run_dir / "proxy-ledger.jsonl").rows
    candidates: list[tuple[TrialRow, float]] = []
    for location in ("harbor-job", "set-aside-trials", "canary-job"):
        for trial_dir in trial_folders(run_dir / location):
            facts = read_trial(trial_dir)
            verdict = verdicts.get(facts.trial)
            if verdict is None:
                continue
            details = read_trial_details(trial_dir)
            arm = arms[facts.arm]
            trial_ledger = [row for row in ledger_rows if row.get("trial") == facts.trial]
            starts = [row for row in trial_ledger if row.get("type") == "request_start"]
            ends = [row for row in trial_ledger if row.get("type") == "request_end"]
            ok_ends = [row for row in ends if row["upstream_status"] == "ok"]
            prompt_tokens = sum(row["prompt_tokens"] or 0 for row in ends)
            completion_tokens = sum(row["completion_tokens"] or 0 for row in ends)
            cached_read_tokens = sum(row["cached_read_tokens"] or 0 for row in ends)
            pricing = metadata["model_card"]["pricing"]
            cost_pricing = {
                "price_in": pricing["input"],
                "price_out": pricing["output"],
                "price_cache": pricing["cached_read"],
            }
            duration = sum(row["duration_s"] for row in ok_ends)
            ok_completion_tokens = sum(row["completion_tokens"] or 0 for row in ok_ends)
            first_contents = [
                row["first_content_s"] for row in ok_ends if row["first_content_s"] is not None
            ]
            row = TrialRow(
                trial=facts.trial,
                task=facts.task,
                arm=facts.arm,
                harness=arm["harness"],
                commit=arm["commit"],
                model=metadata["model_card"]["id"],
                location=location,
                is_first_attempt=False,
                is_final=location == "harbor-job" and verdict["valid"],
                reward=facts.reward,
                is_pass=facts.reward == 1.0,
                harbor_exception=details.harbor_exception,
                agent_exit_nonzero=details.agent_exit_nonzero,
                environment_setup_started_t=facts.phase_started["environment_setup"],
                environment_setup_finished_t=facts.phase_finished["environment_setup"],
                agent_setup_started_t=facts.phase_started["agent_setup"],
                agent_setup_finished_t=facts.phase_finished["agent_setup"],
                agent_started_t=facts.phase_started["agent"],
                agent_finished_t=facts.phase_finished["agent"],
                verifier_started_t=facts.phase_started["verifier"],
                verifier_finished_t=facts.phase_finished["verifier"],
                valid=verdict["valid"],
                reason=verdict["reason"],
                instrument=verdict["instrument"],
                labels=verdict["labels"],
                measured=verdict["measured"],
                unmeasured_reason=verdict["unmeasured_reason"],
                requests=len(ends),
                error_requests=sum(row["upstream_status"] in COUNTS_AGAINST_TRIAL for row in ends),
                refused_requests=sum(row.get("type") == "refused" for row in trial_ledger),
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                cached_read_tokens=cached_read_tokens,
                prompt_chars=sum(row["prompt_chars"] for row in starts),
                completion_chars=sum(row["completion_chars"] for row in ends),
                cost_usd_estimate=cost_usd(
                    cost_pricing, prompt_tokens, completion_tokens, cached_read_tokens
                ),
                harbor_reported_tokens=facts.harbor_reported_tokens,
                turns=details.turns,
                tool_calls=details.tool_calls,
                median_first_content_s=median(first_contents) if first_contents else None,
                output_rate_tokens_per_s=ok_completion_tokens / duration if duration else None,
                agent_wall_s=details.agent_wall_s,
            )
            attempt_time = facts.phase_started["agent"]
            if attempt_time is None:
                attempt_time = facts.phase_started["environment_setup"]
            candidates.append((row, attempt_time if attempt_time is not None else float("inf")))

    repetitions = metadata["job"]["repetitions"]
    attempts: dict[tuple[str, str], list[int]] = {}
    for index, (row, _timestamp) in enumerate(candidates):
        if row.location != "canary-job":
            attempts.setdefault((row.task, row.arm), []).append(index)
    for indices in attempts.values():
        ordered = sorted(
            indices, key=lambda index: (candidates[index][1], candidates[index][0].trial)
        )
        for index in ordered[:repetitions]:
            candidates[index] = (
                replace(candidates[index][0], is_first_attempt=True),
                candidates[index][1],
            )

    rows = [row for row, _ in candidates]
    return sorted(
        rows,
        key=lambda row: (
            row.task,
            row.arm,
            row.agent_started_t if row.agent_started_t is not None else float("inf"),
            row.trial,
        ),
    )
