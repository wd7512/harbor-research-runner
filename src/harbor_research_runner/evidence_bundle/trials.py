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
from harbor_research_runner.records.jsonl import bad_line_time_spans, read_rows
from harbor_research_runner.records.memory_watch import memory_watch_stats
from harbor_research_runner.records.proxy_ledger import (
    COUNTS_AGAINST_TRIAL,
    is_valid_row,
)
from harbor_research_runner.records.run_events import final_verdicts, telemetry_finished
from harbor_research_runner.records.trials import TrialRow
from harbor_research_runner.statistics.cost import cost_usd


def unjudged_trial_names(run_dir: Path) -> set[str]:
    verdicts = {
        row["trial"]
        for row in read_rows(run_dir / "run-events.jsonl").rows
        if row.get("type") == "trial_verdict"
    }
    return {
        facts.trial
        for location in ("harbor-job", "set-aside-trials", "canary-job")
        for trial_dir in trial_folders(run_dir / location)
        for facts in (read_trial(trial_dir),)
        if facts.trial not in verdicts
    }


def build_trial_rows(run_dir: Path) -> list[TrialRow]:
    metadata = json.loads((run_dir / "run-metadata.json").read_text(encoding="utf-8"))
    arms = {arm["label"]: arm for arm in metadata["arms"]}
    events = read_rows(run_dir / "run-events.jsonl").rows
    verdicts = final_verdicts(events)
    ledger_rows = [
        row for row in read_rows(run_dir / "proxy-ledger.jsonl").rows if is_valid_row(row)
    ]
    memory_path = run_dir / "memory-watch.jsonl"
    memory_result = read_rows(memory_path)
    memory_rows = memory_result.rows
    memory_bad_spans = bad_line_time_spans(memory_result, finished=telemetry_finished(events))
    memory_stats = memory_watch_stats(memory_rows)
    machine_check = json.loads((run_dir / "machine-check.json").read_text(encoding="utf-8"))
    task_memory = {row["name"]: row.get("memory_mb") for row in machine_check["tasks"]}
    sample_locations = {"set-aside-trials": "harbor-job"}
    candidates: list[tuple[TrialRow, float]] = []
    missing_verdicts: set[str] = set()
    for location in ("harbor-job", "set-aside-trials", "canary-job"):
        for trial_dir in trial_folders(run_dir / location):
            facts = read_trial(trial_dir)
            sample_location = sample_locations.get(location, location)
            verdict = verdicts.get(facts.trial)
            if verdict is None:
                missing_verdicts.add(facts.trial)
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
            priced_ends = [row for row in ends if row["prompt_tokens"] is not None]
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
            agent_start = facts.phase_started.get("agent")
            trial_end = (
                facts.phase_finished.get("verifier")
                if facts.phase_started.get("verifier") is not None
                else facts.phase_finished.get("agent")
            )
            memory_damaged = (
                agent_start is not None
                and trial_end is not None
                and any(
                    start <= trial_end and end >= agent_start for start, end in memory_bad_spans
                )
            )
            row = TrialRow(
                trial=facts.trial,
                task=facts.task,
                arm=facts.arm,
                harness=arm["harness"],
                commit=arm["commit"],
                prompting=arm.get("prompting", "direct"),
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
                oom_source=verdict.get("oom_source"),
                peak_memory_mb=(
                    None
                    if memory_damaged
                    else memory_stats.get((sample_location, facts.trial), (None, None))[0]
                ),
                memory_limit_mb=(
                    memory_stats.get((sample_location, facts.trial), (None, None))[1]
                    if sample_location == "canary-job"
                    else task_memory.get(facts.task)
                ),
                requests=len(ends),
                error_requests=sum(row["upstream_status"] in COUNTS_AGAINST_TRIAL for row in ends),
                refused_requests=sum(row.get("type") == "refused" for row in trial_ledger),
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                cached_read_tokens=cached_read_tokens,
                prompt_chars=sum(row["prompt_chars"] for row in starts),
                completion_chars=sum(row["completion_chars"] for row in ends),
                cost_usd_estimate=cost_usd(
                    cost_pricing,
                    sum(row["prompt_tokens"] for row in priced_ends),
                    sum(row["completion_tokens"] or 0 for row in priced_ends),
                    sum(row["cached_read_tokens"] or 0 for row in priced_ends),
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

    if missing_verdicts and any(row.get("type") == "finish" for row in events):
        missing = ", ".join(sorted(missing_verdicts))
        raise ValueError(f"trial folders without trial_verdict events: {missing}")

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
