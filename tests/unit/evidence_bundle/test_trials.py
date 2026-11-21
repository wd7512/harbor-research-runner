import json
import shutil
from pathlib import Path

from harbor_research_runner.evidence_bundle.trials import (
    build_trial_rows,
    unjudged_trial_names,
)
from harbor_research_runner.run.canary import canary_memory_mb

FIXTURES = Path(__file__).parents[2] / "fixtures" / "run_folders"


def _expected(folder: Path) -> list[dict]:
    rows = [
        json.loads(line) for line in (folder / "expected-trials.jsonl").read_text().splitlines()
    ]
    machine_check = json.loads((folder / "machine-check.json").read_text(encoding="utf-8"))
    task_memory = {row["name"]: row.get("memory_mb") for row in machine_check["tasks"]}
    for row in rows:
        row["prompting"] = "direct"
        row["oom_source"] = None
        row["peak_memory_mb"] = None
        row["memory_limit_mb"] = task_memory.get(row["task"])
    return rows


def test_clean_run_trial_rows_match_expected() -> None:
    folder = FIXTURES / "clean"
    assert [row.to_row() for row in build_trial_rows(folder)] == _expected(folder)


def test_valid_set_aside_and_unmeasured_trial_rows_match_expected(tmp_path: Path) -> None:
    folder = tmp_path / "retries"
    shutil.copytree(FIXTURES / "retries", folder)
    assert [row.to_row() for row in build_trial_rows(folder)] == _expected(folder)


def test_trial_usage_sums_known_tokens_and_preserves_incomplete_reason(tmp_path: Path) -> None:
    folder = tmp_path / "clean"
    shutil.copytree(FIXTURES / "clean", folder)
    ledger_path = folder / "proxy-ledger.jsonl"
    ledger = [json.loads(line) for line in ledger_path.read_text(encoding="utf-8").splitlines()]
    end = next(
        row
        for row in ledger
        if row.get("type") == "request_end" and row.get("trial") == "a-alpha-trial"
    )
    end["prompt_tokens"] = None
    end["cached_read_tokens"] = None
    ledger_path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in ledger),
        encoding="utf-8",
    )
    events_path = folder / "run-events.jsonl"
    events = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines()]
    verdict = next(
        row
        for row in events
        if row.get("type") == "trial_verdict" and row.get("trial") == "a-alpha-trial"
    )
    verdict.update(measured=False, unmeasured_reason="usage_incomplete")
    events_path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in events),
        encoding="utf-8",
    )

    row = next(row for row in build_trial_rows(folder) if row.trial == "a-alpha-trial")

    assert row.prompt_tokens == 0
    assert row.completion_tokens == 4
    assert not row.measured
    assert row.unmeasured_reason == "usage_incomplete"


def test_set_aside_trial_uses_memory_sample_from_harbor_job(tmp_path: Path) -> None:
    folder = tmp_path / "retries"
    shutil.copytree(FIXTURES / "retries", folder)
    (folder / "memory-watch.jsonl").write_text(
        json.dumps(
            {
                "type": "sample",
                "t": 10.0,
                "trial": "a-alpha-old",
                "location": "harbor-job",
                "container": "container-a",
                "used_mb": 123.5,
                "limit_mb": 256,
                "reader": "docker_stats",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    row = next(row for row in build_trial_rows(folder) if row.trial == "a-alpha-old")

    assert row.peak_memory_mb == 123.5


def test_every_slot_has_k_first_attempts() -> None:
    rows = build_trial_rows(FIXTURES / "clean")
    slots = {(row.task, row.arm) for row in rows if row.location != "canary-job"}
    assert all(
        sum(row.is_first_attempt for row in rows if (row.task, row.arm) == slot) == 1
        for slot in slots
    )


def test_trial_folder_without_verdict_is_skipped() -> None:
    rows = build_trial_rows(FIXTURES / "retries")
    assert all(row.trial != "unfinished" for row in rows)


def test_canary_is_flagged_and_not_a_first_attempt() -> None:
    rows = build_trial_rows(FIXTURES / "clean")
    canaries = [row for row in rows if row.location == "canary-job"]
    assert canaries
    assert all(not row.is_first_attempt and not row.is_final for row in canaries)


def test_reading_same_run_twice_gives_identical_rows() -> None:
    folder = FIXTURES / "clean"
    assert build_trial_rows(folder) == build_trial_rows(folder)


def test_legacy_run_metadata_without_prompting_reads_as_direct() -> None:
    rows = build_trial_rows(FIXTURES / "clean")

    assert rows
    assert all(row.prompting == "direct" for row in rows)


def test_run_metadata_prompting_is_copied_to_trial_rows(tmp_path: Path) -> None:
    folder = tmp_path / "clean"
    shutil.copytree(FIXTURES / "clean", folder)
    metadata_path = folder / "run-metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["arms"][0]["prompting"] = "plan-first"
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    rows = build_trial_rows(folder)

    assert all(row.prompting == "plan-first" for row in rows if row.arm == "alpha")


def test_trial_rows_include_memory_peak_and_task_limit(tmp_path: Path) -> None:
    folder = tmp_path / "clean"
    shutil.copytree(FIXTURES / "clean", folder)
    initial_rows = build_trial_rows(folder)
    trial = next(row for row in initial_rows if row.location == "harbor-job")
    machine_check_path = folder / "machine-check.json"
    machine_check = json.loads(machine_check_path.read_text(encoding="utf-8"))
    task = next(row for row in machine_check["tasks"] if row["name"] == trial.task)
    task["memory_mb"] = 128
    machine_check_path.write_text(json.dumps(machine_check), encoding="utf-8")
    (folder / "memory-watch.jsonl").write_text(
        json.dumps(
            {
                "type": "sample",
                "t": 10.0,
                "trial": trial.trial,
                "location": "harbor-job",
                "container": "container-a",
                "used_mb": 42.5,
                "limit_mb": 128,
                "reader": "docker_stats",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    measured = next(row for row in build_trial_rows(folder) if row.trial == trial.trial)

    assert measured.peak_memory_mb == 42.5
    assert measured.memory_limit_mb == 128
    assert measured.oom_source is None


def test_memory_damage_in_trial_window_nulls_peak_memory(tmp_path: Path) -> None:
    folder = tmp_path / "clean"
    shutil.copytree(FIXTURES / "clean", folder)
    target = next(row for row in build_trial_rows(folder) if row.location == "harbor-job")
    first = target.agent_started_t + 1
    second = target.agent_finished_t - 1
    samples = [
        {
            "type": "sample",
            "t": first,
            "trial": target.trial,
            "location": "harbor-job",
            "container": "container-a",
            "used_mb": 42.5,
            "limit_mb": 128,
            "reader": "docker_stats",
        },
        '{"t":',
        {
            "type": "sample",
            "t": second,
            "trial": target.trial,
            "location": "harbor-job",
            "container": "container-a",
            "used_mb": 48.5,
            "limit_mb": 128,
            "reader": "docker_stats",
        },
    ]
    lines = [
        item if isinstance(item, str) else json.dumps(item, sort_keys=True) for item in samples
    ]
    (folder / "memory-watch.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")

    row = next(row for row in build_trial_rows(folder) if row.trial == target.trial)

    assert row.peak_memory_mb is None


def test_canary_trial_evidence_uses_its_recorded_memory_limit(tmp_path: Path) -> None:
    folder = tmp_path / "clean"
    shutil.copytree(FIXTURES / "clean", folder)
    canary = next(row for row in build_trial_rows(folder) if row.location == "canary-job")
    (folder / "memory-watch.jsonl").write_text(
        json.dumps(
            {
                "type": "sample",
                "t": 10.0,
                "trial": canary.trial,
                "location": "canary-job",
                "container": "container-a",
                "used_mb": 16,
                "limit_mb": canary_memory_mb(),
                "reader": "docker_stats",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    measured = next(row for row in build_trial_rows(folder) if row.trial == canary.trial)

    assert measured.peak_memory_mb == 16
    assert measured.memory_limit_mb == canary_memory_mb()


def test_unjudged_trial_names_covers_main_set_aside_and_canary(tmp_path: Path) -> None:
    folder = tmp_path / "run"
    shutil.copytree(FIXTURES / "clean", folder)
    events_path = folder / "run-events.jsonl"
    events = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines()]
    omitted = {"a-alpha-trial", "canary-alpha", "set-aside-alpha"}
    events = [
        row
        for row in events
        if row.get("type") != "trial_verdict" or row.get("trial") not in omitted
    ]
    events_path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in events),
        encoding="utf-8",
    )
    set_aside = folder / "set-aside-trials" / "set-aside-alpha"
    shutil.copytree(folder / "harbor-job" / "a-alpha-trial", set_aside)
    result_path = set_aside / "result.json"
    result = json.loads(result_path.read_text(encoding="utf-8"))
    result["trial_name"] = "set-aside-alpha"
    result_path.write_text(json.dumps(result), encoding="utf-8")

    assert unjudged_trial_names(folder) == omitted


def test_trial_rows_sum_zero_completion_tokens(tmp_path: Path) -> None:
    folder = tmp_path / "run"
    shutil.copytree(FIXTURES / "clean", folder)
    trial = "a-alpha-trial"
    ledger_path = folder / "proxy-ledger.jsonl"
    ledger = [json.loads(line) for line in ledger_path.read_text(encoding="utf-8").splitlines()]
    end = next(
        row for row in ledger if row.get("type") == "request_end" and row.get("trial") == trial
    )
    end["completion_tokens"] = 0
    ledger_path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in ledger),
        encoding="utf-8",
    )

    row = next(row for row in build_trial_rows(folder) if row.trial == trial)

    assert row.completion_tokens == 0
    assert row.output_rate_tokens_per_s == 0.0


def test_trial_rows_count_closed_refusals(tmp_path: Path) -> None:
    folder = tmp_path / "run"
    shutil.copytree(FIXTURES / "clean", folder)
    trial = "a-alpha-trial"
    ledger_path = folder / "proxy-ledger.jsonl"
    ledger = [json.loads(line) for line in ledger_path.read_text(encoding="utf-8").splitlines()]
    ledger.append(
        {
            "type": "refused",
            "seq": max(row["seq"] for row in ledger) + 1,
            "t": 20.0,
            "trial": trial,
            "reason": "closed",
        }
    )
    ledger_path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in ledger),
        encoding="utf-8",
    )

    row = next(row for row in build_trial_rows(folder) if row.trial == trial)

    assert row.refused_requests == 1


def test_trial_memory_peak_ignores_invalid_rows_and_uses_the_maximum(tmp_path: Path) -> None:
    folder = tmp_path / "clean"
    shutil.copytree(FIXTURES / "clean", folder)
    target = next(row for row in build_trial_rows(folder) if row.location == "harbor-job")
    samples = [
        {"type": "other", "t": 1.0, "trial": None, "location": None},
        {
            "type": "over_limit",
            "t": 1.5,
            "trial": target.trial,
            "location": "harbor-job",
            "container": "container-a",
            "used_mb": 50,
            "limit_mb": 128,
            "reader": "docker_stats",
        },
        {
            "type": "sample",
            "t": 2.0,
            "trial": target.trial,
            "location": "harbor-job",
            "container": "container-a",
            "used_mb": 48.5,
            "limit_mb": 128,
            "reader": "docker_stats",
        },
        {
            "type": "sample",
            "t": 3.0,
            "trial": target.trial,
            "location": "harbor-job",
            "container": "container-a",
            "used_mb": 42.5,
            "limit_mb": 128,
            "reader": "docker_stats",
        },
        {
            "type": "sample",
            "t": 4.0,
            "trial": target.trial,
            "location": "harbor-job",
            "container": "container-a",
            "used_mb": "invalid",
            "limit_mb": 128,
            "reader": "docker_stats",
        },
    ]
    (folder / "memory-watch.jsonl").write_text(
        "".join(json.dumps(sample, sort_keys=True) + "\n" for sample in samples),
        encoding="utf-8",
    )

    row = next(row for row in build_trial_rows(folder) if row.trial == target.trial)

    assert row.peak_memory_mb == 48.5


def test_canary_trial_ignores_a_non_integer_recorded_memory_limit(tmp_path: Path) -> None:
    folder = tmp_path / "clean"
    shutil.copytree(FIXTURES / "clean", folder)
    canary = next(row for row in build_trial_rows(folder) if row.location == "canary-job")
    (folder / "memory-watch.jsonl").write_text(
        json.dumps(
            {
                "type": "sample",
                "t": 10.0,
                "trial": canary.trial,
                "location": "canary-job",
                "container": "container-a",
                "used_mb": 16,
                "limit_mb": "invalid",
                "reader": "docker_stats",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    row = next(row for row in build_trial_rows(folder) if row.trial == canary.trial)

    assert row.memory_limit_mb is None


def test_memory_damage_after_agent_end_still_clears_the_peak(tmp_path: Path) -> None:
    folder = tmp_path / "clean"
    shutil.copytree(FIXTURES / "clean", folder)
    target = next(row for row in build_trial_rows(folder) if row.location == "harbor-job")
    times = [target.agent_finished_t + 0.25, target.verifier_finished_t - 0.25]
    samples = [
        {
            "type": "sample",
            "t": times[0],
            "trial": target.trial,
            "location": "harbor-job",
            "container": "container-a",
            "used_mb": 42.5,
            "limit_mb": 128,
            "reader": "docker_stats",
        },
        '{"t":',
        {
            "type": "sample",
            "t": times[1],
            "trial": target.trial,
            "location": "harbor-job",
            "container": "container-a",
            "used_mb": 48.5,
            "limit_mb": 128,
            "reader": "docker_stats",
        },
    ]
    lines = [item if isinstance(item, str) else json.dumps(item) for item in samples]
    (folder / "memory-watch.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")

    row = next(row for row in build_trial_rows(folder) if row.trial == target.trial)

    assert row.peak_memory_mb is None


def test_memory_damage_span_touching_agent_start_clears_the_peak(tmp_path: Path) -> None:
    folder = tmp_path / "clean"
    shutil.copytree(FIXTURES / "clean", folder)
    target = next(row for row in build_trial_rows(folder) if row.location == "harbor-job")
    samples = [
        {
            "type": "sample",
            "t": target.agent_started_t,
            "trial": target.trial,
            "location": "harbor-job",
            "container": "container-a",
            "used_mb": 42.5,
            "limit_mb": 128,
            "reader": "docker_stats",
        },
        '{"t":',
        {
            "type": "sample",
            "t": target.agent_started_t,
            "trial": target.trial,
            "location": "harbor-job",
            "container": "container-a",
            "used_mb": 48.5,
            "limit_mb": 128,
            "reader": "docker_stats",
        },
    ]
    lines = [item if isinstance(item, str) else json.dumps(item) for item in samples]
    (folder / "memory-watch.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")

    row = next(row for row in build_trial_rows(folder) if row.trial == target.trial)

    assert row.peak_memory_mb is None


def test_memory_damage_span_touching_trial_end_clears_the_peak(tmp_path: Path) -> None:
    folder = tmp_path / "clean"
    shutil.copytree(FIXTURES / "clean", folder)
    target = next(row for row in build_trial_rows(folder) if row.location == "harbor-job")
    samples = [
        {
            "type": "sample",
            "t": target.verifier_finished_t,
            "trial": target.trial,
            "location": "harbor-job",
            "container": "container-a",
            "used_mb": 42.5,
            "limit_mb": 128,
            "reader": "docker_stats",
        },
        '{"t":',
        {
            "type": "sample",
            "t": target.verifier_finished_t,
            "trial": target.trial,
            "location": "harbor-job",
            "container": "container-a",
            "used_mb": 48.5,
            "limit_mb": 128,
            "reader": "docker_stats",
        },
    ]
    lines = [item if isinstance(item, str) else json.dumps(item) for item in samples]
    (folder / "memory-watch.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")

    row = next(row for row in build_trial_rows(folder) if row.trial == target.trial)

    assert row.peak_memory_mb is None


def test_memory_damage_before_agent_start_does_not_clear_the_peak(tmp_path: Path) -> None:
    folder = tmp_path / "clean"
    shutil.copytree(FIXTURES / "clean", folder)
    target = next(row for row in build_trial_rows(folder) if row.location == "harbor-job")
    samples = [
        {
            "type": "sample",
            "t": target.agent_started_t - 2,
            "trial": target.trial,
            "location": "harbor-job",
            "container": "container-a",
            "used_mb": 50,
            "limit_mb": 128,
            "reader": "docker_stats",
        },
        '{"t":',
        {
            "type": "sample",
            "t": target.agent_started_t - 1,
            "trial": target.trial,
            "location": "harbor-job",
            "container": "container-a",
            "used_mb": 49,
            "limit_mb": 128,
            "reader": "docker_stats",
        },
        {
            "type": "sample",
            "t": target.agent_started_t + 1,
            "trial": target.trial,
            "location": "harbor-job",
            "container": "container-a",
            "used_mb": 42.5,
            "limit_mb": 128,
            "reader": "docker_stats",
        },
    ]
    lines = [item if isinstance(item, str) else json.dumps(item) for item in samples]
    (folder / "memory-watch.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")

    row = next(row for row in build_trial_rows(folder) if row.trial == target.trial)

    assert row.peak_memory_mb == 50


def test_memory_damage_uses_agent_end_when_verifier_never_started(tmp_path: Path) -> None:
    folder = tmp_path / "clean"
    shutil.copytree(FIXTURES / "clean", folder)
    target = next(row for row in build_trial_rows(folder) if row.location == "harbor-job")
    result_path = folder / "harbor-job" / target.trial / "result.json"
    result = json.loads(result_path.read_text(encoding="utf-8"))
    result.pop("verifier")
    result_path.write_text(json.dumps(result), encoding="utf-8")
    samples = [
        {
            "type": "sample",
            "t": target.agent_started_t + 1,
            "trial": target.trial,
            "location": "harbor-job",
            "container": "container-a",
            "used_mb": 42.5,
            "limit_mb": 128,
            "reader": "docker_stats",
        },
        '{"t":',
        {
            "type": "sample",
            "t": target.agent_finished_t - 1,
            "trial": target.trial,
            "location": "harbor-job",
            "container": "container-a",
            "used_mb": 48.5,
            "limit_mb": 128,
            "reader": "docker_stats",
        },
    ]
    lines = [item if isinstance(item, str) else json.dumps(item) for item in samples]
    (folder / "memory-watch.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")

    row = next(row for row in build_trial_rows(folder) if row.trial == target.trial)

    assert row.verifier_started_t is None
    assert row.peak_memory_mb is None


def test_trial_rows_preserve_exception_and_trajectory_details(tmp_path: Path) -> None:
    folder = tmp_path / "clean"
    shutil.copytree(FIXTURES / "clean", folder)
    trial_dir = folder / "harbor-job" / "a-alpha-trial"
    result_path = trial_dir / "result.json"
    result = json.loads(result_path.read_text(encoding="utf-8"))
    result["exception_info"] = {
        "exception_type": "AgentError",
        "occurred_at": "2026-01-01T00:00:12Z",
    }
    result_path.write_text(json.dumps(result), encoding="utf-8")
    trajectory_path = trial_dir / "agent" / "trajectory.json"
    trajectory_path.parent.mkdir()
    trajectory_path.write_text(
        json.dumps(
            {
                "steps": [{"tool_calls": [{"function_name": "search"}]}],
                "final_metrics": {"total_steps": 3},
            }
        ),
        encoding="utf-8",
    )
    row = next(row for row in build_trial_rows(folder) if row.trial == "a-alpha-trial")

    assert row.harbor_exception == "AgentError"
    assert row.agent_exit_nonzero
    assert row.turns == 3
    assert row.tool_calls == 1


def test_trial_rows_preserve_oom_source(tmp_path: Path) -> None:
    folder = tmp_path / "clean"
    shutil.copytree(FIXTURES / "clean", folder)
    events_path = folder / "run-events.jsonl"
    events = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines()]
    verdict = next(row for row in events if row.get("trial") == "a-alpha-trial")
    verdict["oom_source"] = "watchdog"
    events_path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in events),
        encoding="utf-8",
    )

    row = next(row for row in build_trial_rows(folder) if row.trial == "a-alpha-trial")

    assert row.oom_source == "watchdog"


def test_first_attempt_uses_agent_start_time_before_environment_setup_time(
    tmp_path: Path,
) -> None:
    folder = tmp_path / "retries"
    shutil.copytree(FIXTURES / "retries", folder)
    old_result_path = folder / "set-aside-trials" / "a-alpha-old" / "result.json"
    old_result = json.loads(old_result_path.read_text(encoding="utf-8"))
    old_result["agent_execution"]["started_at"] = "2026-01-01T00:00:14Z"
    old_result_path.write_text(json.dumps(old_result), encoding="utf-8")
    retry_result_path = folder / "harbor-job" / "a-alpha-trial" / "result.json"
    retry_result = json.loads(retry_result_path.read_text(encoding="utf-8"))
    retry_result["agent_execution"]["started_at"] = "2026-01-01T00:00:12Z"
    retry_result_path.write_text(json.dumps(retry_result), encoding="utf-8")
    unmeasured_result_path = folder / "harbor-job" / "unmeasured" / "result.json"
    unmeasured_result = json.loads(unmeasured_result_path.read_text(encoding="utf-8"))
    unmeasured_result["agent_execution"]["started_at"] = "2026-01-01T00:00:16Z"
    unmeasured_result_path.write_text(json.dumps(unmeasured_result), encoding="utf-8")

    rows = build_trial_rows(folder)

    assert next(row for row in rows if row.trial == "a-alpha-trial").is_first_attempt
    assert not next(row for row in rows if row.trial == "a-alpha-old").is_first_attempt


def test_first_attempt_falls_back_to_environment_setup_when_agent_never_started(
    tmp_path: Path,
) -> None:
    folder = tmp_path / "retries"
    shutil.copytree(FIXTURES / "retries", folder)
    old_result_path = folder / "set-aside-trials" / "a-alpha-old" / "result.json"
    old_result = json.loads(old_result_path.read_text(encoding="utf-8"))
    old_result["agent_execution"].pop("started_at")
    old_result["environment_setup"]["started_at"] = "2026-01-01T00:00:11Z"
    old_result_path.write_text(json.dumps(old_result), encoding="utf-8")
    retry_result_path = folder / "harbor-job" / "a-alpha-trial" / "result.json"
    retry_result = json.loads(retry_result_path.read_text(encoding="utf-8"))
    retry_result["agent_execution"]["started_at"] = "2026-01-01T00:00:12Z"
    retry_result_path.write_text(json.dumps(retry_result), encoding="utf-8")
    unmeasured_result_path = folder / "harbor-job" / "unmeasured" / "result.json"
    unmeasured_result = json.loads(unmeasured_result_path.read_text(encoding="utf-8"))
    unmeasured_result["agent_execution"]["started_at"] = "2026-01-01T00:00:16Z"
    unmeasured_result_path.write_text(json.dumps(unmeasured_result), encoding="utf-8")

    rows = build_trial_rows(folder)

    assert next(row for row in rows if row.trial == "a-alpha-old").is_first_attempt
    assert not next(row for row in rows if row.trial == "a-alpha-trial").is_first_attempt


def test_trial_rows_sort_without_an_agent_start_time(tmp_path: Path) -> None:
    folder = tmp_path / "clean"
    shutil.copytree(FIXTURES / "clean", folder)
    trial = "a-alpha-trial"
    result_path = folder / "harbor-job" / trial / "result.json"
    result = json.loads(result_path.read_text(encoding="utf-8"))
    result["agent_execution"].pop("started_at")
    result["exception_info"] = {
        "exception_type": "AgentSetupError",
        "occurred_at": "2026-01-01T00:00:09Z",
    }
    result_path.write_text(json.dumps(result), encoding="utf-8")

    rows = build_trial_rows(folder)

    assert next(row for row in rows if row.trial == trial).agent_started_t is None


def test_trial_rows_sort_when_no_phase_has_started(tmp_path: Path) -> None:
    folder = tmp_path / "clean"
    shutil.copytree(FIXTURES / "clean", folder)
    trial = "a-alpha-trial"
    result_path = folder / "harbor-job" / trial / "result.json"
    result = json.loads(result_path.read_text(encoding="utf-8"))
    for phase in ("environment_setup", "agent_setup", "agent_execution", "verifier"):
        result.pop(phase)
    result_path.write_text(json.dumps(result), encoding="utf-8")

    rows = build_trial_rows(folder)

    row = next(row for row in rows if row.trial == trial)
    assert row.environment_setup_started_t is None
    assert row.agent_started_t is None
