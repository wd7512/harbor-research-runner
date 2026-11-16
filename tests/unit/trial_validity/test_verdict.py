import json
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import pytest

from harbor_research_runner.records.jsonl import ReadResult, read_rows
from harbor_research_runner.records.limits import Limits
from harbor_research_runner.trial_validity.verdict import TrialFacts, judge_trial

TRIAL = "task__abc123"
DEFAULT_LIMITS = Limits.from_dict({}, repetitions=1, verifier_timeout_multiplier=1.5)


def _phases(
    env=(1.0, 1.5),
    setup=(2.0, 2.5),
    agent=(3.0, 5.0),
    verifier=(None, None),
):
    return (
        {
            "environment_setup": env[0],
            "agent_setup": setup[0],
            "agent": agent[0],
            "verifier": verifier[0],
        },
        {
            "environment_setup": env[1],
            "agent_setup": setup[1],
            "agent": agent[1],
            "verifier": verifier[1],
        },
    )


def _facts(**overrides):
    phase_started, phase_finished = _phases()
    values = {
        "trial": TRIAL,
        "task": "task",
        "arm": "oc-1",
        "has_result": True,
        "reward": 1.0,
        "exception_type": None,
        "exception_at": None,
        "phase_started": phase_started,
        "phase_finished": phase_finished,
        "harbor_reported_tokens": 100,
    }
    values.update(overrides)
    return TrialFacts(**values)


def _open_row(seq, trial=TRIAL):
    return {
        "type": "open",
        "seq": seq,
        "t": float(seq),
        "trial": trial,
        "task": "task",
        "arm": "oc-1",
    }


def _request_start(seq, request_id, trial=TRIAL):
    return {
        "type": "request_start",
        "seq": seq,
        "t": float(seq),
        "trial": trial,
        "request_id": request_id,
        "model": "model",
        "is_stream": True,
        "prompt_chars": 100,
        "paced_wait_s": 0.0,
    }


def _request_end(
    seq,
    request_id,
    status="ok",
    completion_tokens=100,
    duration_s=1.0,
    trial=TRIAL,
):
    return {
        "type": "request_end",
        "seq": seq,
        "t": float(seq),
        "trial": trial,
        "request_id": request_id,
        "http_status": 200,
        "upstream_status": status,
        "upstream_http_status": 200,
        "served_model": "model",
        "prompt_tokens": 100,
        "completion_tokens": completion_tokens,
        "cached_read_tokens": 0,
        "completion_chars": 100,
        "first_chunk_s": 0.1,
        "first_content_s": 0.2,
        "duration_s": duration_s,
        "failovers": 0,
        "error_excerpt": None,
    }


def _close_row(seq, trial=TRIAL):
    return {"type": "close", "seq": seq, "t": float(seq), "trial": trial}


def _refused(seq, reason, trial=TRIAL):
    return {"type": "refused", "seq": seq, "t": float(seq), "trial": trial, "reason": reason}


def _sample(t, mem_available_mb=8192, swapped_out_pages=0, disk_free_gb=100, docker_ok=True):
    return {
        "type": "sample",
        "t": t,
        "mem_available_mb": mem_available_mb,
        "swapped_out_pages": swapped_out_pages,
        "disk_free_gb": disk_free_gb,
        "load1": 0.5,
        "trial_containers": 1,
        "docker_ok": docker_ok,
    }


def _ledger_from_ends(ends):
    rows = [_open_row(1)]
    seq = 2
    for index, end in enumerate(ends):
        rows.append(_request_start(seq, f"r{index}"))
        seq += 1
        rows.append(_request_end(seq, f"r{index}", **end))
        seq += 1
    rows.append(_close_row(seq))
    return rows


def _requests(statuses):
    return _ledger_from_ends([{"status": status} for status in statuses])


def _clean_ledger():
    return _requests(["ok"])


def _write_ledger(tmp_path, entries):
    lines = [
        entry if isinstance(entry, str) else json.dumps(entry, sort_keys=True) for entry in entries
    ]
    path = tmp_path / "proxy-ledger.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return read_rows(path)


def _judge(
    facts=None,
    rows=None,
    samples=None,
    earlier=(),
    problems=None,
    oom_events=(),
    memory_watch_events=(),
    resource_problems=None,
):
    facts = _facts() if facts is None else facts
    rows = _clean_ledger() if rows is None else rows
    if samples is None and resource_problems is None:
        samples = [_sample(0.0), _sample(10.0)]
    if resource_problems is None:
        resource_problems = ReadResult(list(samples), False, [])
    if problems is None:
        problems = ReadResult(list(rows), False, [])
    return judge_trial(
        facts,
        rows,
        problems,
        [] if samples is None else samples,
        DEFAULT_LIMITS,
        earlier,
        oom_events
        if isinstance(oom_events, ReadResult)
        else ReadResult(list(oom_events), False, []),
        memory_watch_events
        if isinstance(memory_watch_events, ReadResult)
        else ReadResult(list(memory_watch_events), False, []),
        resource_problems=resource_problems,
        sampler_interval_s=10,
    )


def test_malformed_trial_row_without_an_open_row_is_ledger_damage():
    malformed_start = _request_start(1, "r0")
    del malformed_start["request_id"]

    verdict = _judge(rows=[malformed_start])

    assert verdict.measured is False
    assert verdict.unmeasured_reason == "ledger_damaged"


def test_other_trials_damage_does_not_change_a_trial_without_an_open_row(tmp_path):
    other_start = _request_start(1, "r0", trial="other-trial")
    del other_start["request_id"]
    problems = _write_ledger(
        tmp_path, [other_start, "{not json", _open_row(3, trial="other-trial")]
    )

    verdict = _judge(rows=[], problems=problems)

    assert verdict.measured is False
    assert verdict.unmeasured_reason == "no_open_row"


def test_sequence_gap_ending_at_open_row_without_close_keeps_trial_measured():
    rows = [_open_row(1, trial="other-trial"), _open_row(4)]
    problems = ReadResult(rows, False, [])

    verdict = _judge(
        _facts(has_result=False),
        rows=problems.rows,
        problems=problems,
    )

    assert verdict.measured is True


@pytest.mark.parametrize(
    ("rows", "has_result"),
    [
        ([_open_row(5, trial="other-trial"), _open_row(3)], False),
        ([_open_row(5, trial="other-trial"), _open_row(3), _close_row(4)], True),
    ],
)
def test_sequence_reset_at_open_row_marks_trial_ledger_damaged(rows, has_result):
    problems = ReadResult(rows, False, [])

    verdict = _judge(
        _facts(has_result=has_result),
        rows=problems.rows,
        problems=problems,
    )

    assert verdict.measured is False
    assert verdict.unmeasured_reason == "ledger_damaged"


def test_invalid_other_trial_row_after_close_keeps_trial_measured():
    malformed_start = _request_start(3, "other-request", trial="other-trial")
    del malformed_start["request_id"]
    rows = [_open_row(1), _close_row(2), malformed_start]
    problems = ReadResult(rows, False, [])

    verdict = _judge(rows=problems.rows, problems=problems)

    assert verdict.measured is True


def test_request_start_without_open_or_result_is_no_open_row():
    rows = [_request_start(1, "r0")]
    problems = ReadResult(rows, False, [])

    verdict = _judge(_facts(has_result=False), rows=problems.rows, problems=problems)

    assert verdict.measured is False
    assert verdict.unmeasured_reason == "no_open_row"


def test_damage_after_other_trial_open_inside_trial_window_is_ledger_damaged(tmp_path):
    problems = _write_ledger(
        tmp_path,
        [_open_row(1, trial="other-trial"), _open_row(2), "{not json", _close_row(4)],
    )

    verdict = _judge(rows=problems.rows, problems=problems)

    assert verdict.measured is False
    assert verdict.unmeasured_reason == "ledger_damaged"


def test_damage_before_trial_open_does_not_change_measurement(tmp_path):
    problems = _write_ledger(
        tmp_path,
        [_refused(1, "not_open"), "{not json", _open_row(3), _close_row(4)],
    )

    verdict = _judge(rows=problems.rows, problems=problems)

    assert verdict.measured is True


def test_malformed_oom_line_without_agent_start_returns_verdict(tmp_path):
    started, finished = _phases(agent=(None, 5.0))
    facts = _facts(phase_started=started, phase_finished=finished)
    oom_events = _telemetry_read(tmp_path, "docker-oom-events.jsonl", [], 1)

    verdict = _judge(facts, oom_events=oom_events)

    assert verdict.valid is True
    assert verdict.measured is False
    assert verdict.unmeasured_reason == "phase_times_missing"


def _fixture_oom_facts(name):
    fixture = Path(__file__).parents[2] / "fixtures" / "oom" / f"{name}-result.json"
    result = json.loads(fixture.read_text())
    phases = {
        "environment_setup": "environment_setup",
        "agent_setup": "agent_setup",
        "agent": "agent_execution",
        "verifier": "verifier",
    }

    def stamp(value):
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() if value else None

    return TrialFacts(
        trial=result["trial_name"],
        task=result["task_name"],
        arm="arm",
        has_result=True,
        reward=(result.get("verifier_result") or {}).get("rewards", {}).get("reward"),
        exception_type=(result.get("exception_info") or {}).get("exception_type"),
        exception_at=stamp((result.get("exception_info") or {}).get("occurred_at")),
        phase_started={
            key: stamp(result.get(value, {}).get("started_at")) for key, value in phases.items()
        },
        phase_finished={
            key: stamp(result.get(value, {}).get("finished_at")) for key, value in phases.items()
        },
        harbor_reported_tokens=0,
    )


def _fixture_oom_events():
    path = Path(__file__).parents[2] / "fixtures" / "oom" / "docker-oom-events.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_agent_oom_event_labels_valid_reward_one_trial():
    facts = _fixture_oom_facts("agent")
    verdict = _judge(facts, oom_events=_fixture_oom_events())

    assert verdict.valid
    assert verdict.labels == ("oom_killed", "no_requests")
    assert verdict.oom_source == "kernel"
    assert facts.reward == 1.0


def test_earlier_watchdog_kill_sets_oom_source_and_precedes_kernel_event():
    facts = _facts(exception_type="NonZeroExitError")
    kernel_event = {
        "Actor": {"Attributes": {"com.docker.compose.project": f"{facts.trial.lower()}__env"}},
        "timeNano": 5_000_000_000,
    }
    watchdog_event = {
        "type": "kill",
        "trial": facts.trial,
        "location": "harbor-job",
        "t": 4.0,
        "container": "container-a",
        "used_mb": 101,
        "limit_mb": 100,
        "reader": "proc_rss",
    }

    verdict = _judge(facts, oom_events=[kernel_event], memory_watch_events=[watchdog_event])

    assert verdict.valid
    assert "oom_killed" in verdict.labels
    assert "exit_nonzero" not in verdict.labels
    assert verdict.oom_source == "watchdog"


def test_canary_watchdog_kill_adds_an_oom_label():
    facts = _facts(exception_type="NonZeroExitError")
    watchdog_event = {
        "type": "kill",
        "trial": facts.trial,
        "location": "canary-job",
        "t": 4.0,
        "container": "container-a",
        "used_mb": 513,
        "limit_mb": 512,
        "reader": "docker_stats",
    }

    verdict = _judge(facts, memory_watch_events=[watchdog_event])

    assert verdict.valid
    assert "oom_killed" in verdict.labels
    assert verdict.oom_source == "watchdog"


def test_failed_watchdog_kill_adds_an_oom_label():
    facts = _facts(exception_type="NonZeroExitError")
    watchdog_event = {
        "type": "kill_failed",
        "trial": facts.trial,
        "location": "harbor-job",
        "t": 4.0,
        "container": "container-a",
        "used_mb": 101,
        "limit_mb": 100,
        "reader": "proc_rss",
        "error": "docker kill failed",
    }

    verdict = _judge(facts, memory_watch_events=[watchdog_event])

    assert verdict.valid
    assert "oom_killed" in verdict.labels
    assert verdict.oom_source == "watchdog"


def test_verifier_oom_event_labels_valid_zero_reward_trial():
    facts = _fixture_oom_facts("verifier")
    verdict = _judge(facts, oom_events=_fixture_oom_events())

    assert verdict.valid
    assert verdict.labels == ("oom_killed", "verifier_failed", "no_requests")
    assert facts.reward is None


def test_oom_event_outside_window_does_not_label_trial():
    facts = _facts(exception_type="NonZeroExitError")
    event = {
        "Actor": {"Attributes": {"com.docker.compose.project": f"{facts.trial.lower()}__env"}},
        "timeNano": 1,
    }

    verdict = _judge(facts, oom_events=[event])

    assert "oom_killed" not in verdict.labels


def test_oom_label_takes_precedence_over_agent_exception_label():
    facts = _facts(exception_type="NonZeroExitError")
    event = {
        "Actor": {"Attributes": {"com.docker.compose.project": f"{facts.trial.lower()}__env"}},
        "timeNano": 4_000_000_000,
    }

    verdict = _judge(facts, oom_events=[event])

    assert verdict.valid
    assert "oom_killed" in verdict.labels
    assert "exit_nonzero" not in verdict.labels


def test_oom_event_for_another_trial_does_not_label_trial():
    facts = _facts(exception_type="NonZeroExitError")
    event = {
        "Actor": {"Attributes": {"com.docker.compose.project": "other__env"}},
        "timeNano": 4_000_000_000,
    }

    verdict = _judge(facts, oom_events=[event])

    assert "oom_killed" not in verdict.labels


def _assert_invalid(verdict, reason, instrument):
    assert not verdict.valid
    assert verdict.reason == reason
    assert verdict.instrument == instrument


def test_nonzero_exit_with_reward_one_is_a_valid_pass():
    verdict = _judge(_facts(reward=1.0, exception_type="NonZeroExitError"))

    assert verdict.valid
    assert verdict.reason is None
    assert verdict.instrument is None
    assert set(verdict.labels) == {"exit_nonzero"}
    assert verdict.measured


def test_rate_limit_label_with_clean_ledger_is_valid_failure():
    verdict = _judge(_facts(reward=0.0, exception_type="RateLimitError"))

    assert verdict.valid
    assert verdict.reason is None
    assert set(verdict.labels) == {"exit_nonzero"}
    assert verdict.measured


def test_trial_with_one_tolerated_error_is_valid():
    verdict = _judge(rows=_requests(["server_error"] + ["ok"] * 99))

    assert verdict.valid
    assert verdict.reason is None
    assert verdict.measured


def test_error_on_last_request_invalidates_trial():
    verdict = _judge(rows=_requests(["ok", "server_error"]))

    _assert_invalid(verdict, "upstream_terminal_error", "proxy")


def test_start_without_end_in_closed_trial_is_unmeasured():
    rows = [_open_row(1), _request_start(2, "r1"), _close_row(3)]

    verdict = _judge(rows=rows)

    assert not verdict.measured
    assert verdict.unmeasured_reason == "request_not_recorded"


def test_finished_trial_without_close_row_is_unmeasured():
    rows = [_open_row(1), _request_start(2, "r1"), _request_end(3, "r1")]

    verdict = _judge(_facts(has_result=True), rows=rows)

    assert not verdict.measured
    assert verdict.unmeasured_reason == "close_not_recorded"


def test_damaged_line_marks_trial_unmeasured(tmp_path):
    rows = _clean_ledger()
    result = _write_ledger(tmp_path, [rows[0], rows[1], "not json", rows[2], rows[3]])

    verdict = _judge(rows=result.rows, problems=result)

    assert result.bad_lines == [3]
    assert not verdict.measured
    assert verdict.unmeasured_reason == "ledger_damaged"


def test_damaged_line_after_close_is_ignored(tmp_path):
    rows = _clean_ledger()
    result = _write_ledger(tmp_path, [*rows, "not json", _open_row(6, trial="other")])

    verdict = _judge(rows=result.rows, problems=result)

    assert result.bad_lines == [5]
    assert verdict.measured


def test_shifted_bad_line_inside_trial_marks_it_unmeasured(tmp_path):
    rows = _clean_ledger()
    result = _write_ledger(
        tmp_path,
        ["earlier damage", rows[0], rows[1], rows[2], "inside damage", rows[3]],
    )

    verdict = _judge(rows=result.rows, problems=result)

    assert result.bad_lines == [1, 5]
    assert not verdict.measured
    assert verdict.unmeasured_reason == "ledger_damaged"


def test_bad_line_before_trial_keeps_it_measured(tmp_path):
    rows = _clean_ledger()
    result = _write_ledger(tmp_path, ["first damage", "second damage", *rows])

    verdict = _judge(rows=result.rows, problems=result)

    assert result.bad_lines == [1, 2]
    assert verdict.measured


def test_harbor_tokens_without_proxy_rows_is_possible_bypass():
    verdict = _judge(
        _facts(reward=0.0, harbor_reported_tokens=1),
        rows=[_open_row(1), _close_row(2)],
    )

    assert verdict.valid
    assert "possible_bypass" in verdict.labels
    assert verdict.measured


def test_host_breach_in_agent_window_invalidates_trial():
    samples = [_sample(4.0, mem_available_mb=100)]

    verdict = _judge(samples=samples)

    _assert_invalid(verdict, "host_fault", "host")


@pytest.mark.parametrize("timestamp", [3.0, 5.0])
def test_host_breach_at_agent_window_boundary_invalidates_trial(timestamp):
    verdict = _judge(samples=[_sample(timestamp, mem_available_mb=100)])

    _assert_invalid(verdict, "host_fault", "host")


def test_host_breach_during_verifier_invalidates_trial():
    started, finished = _phases(verifier=(6.0, 8.0))
    facts = _facts(phase_started=started, phase_finished=finished)

    verdict = _judge(facts, samples=[_sample(7.0, mem_available_mb=100)])

    _assert_invalid(verdict, "host_fault", "host")


@pytest.mark.parametrize("timestamp", [True, "bad"])
def test_host_breach_with_invalid_sample_time_is_ignored(timestamp):
    verdict = _judge(samples=[_sample(timestamp, mem_available_mb=100)])

    assert verdict.reason != "host_fault"


def test_host_fault_search_continues_after_invalid_sample_time():
    samples = [
        _sample("bad", mem_available_mb=100),
        _sample(4.0, mem_available_mb=100),
    ]

    verdict = _judge(samples=samples)

    _assert_invalid(verdict, "host_fault", "host")


def test_bad_host_sample_line_overlapping_trial_window_makes_trial_unmeasured():
    samples = ReadResult([_sample(2.0), _sample(6.0)], False, [2])

    verdict = judge_trial(
        _facts(),
        [_open_row(1), _close_row(2)],
        ReadResult([], False, []),
        samples.rows,
        DEFAULT_LIMITS,
        (),
        ReadResult([], False, []),
        ReadResult([], False, []),
        resource_problems=samples,
        sampler_interval_s=10,
    )

    assert verdict.measured is False
    assert verdict.unmeasured_reason == "host_samples_damaged"


def test_host_sample_gap_over_three_intervals_makes_trial_unmeasured():
    samples = [_sample(0.0), _sample(40.0)]

    verdict = _judge(samples=samples, resource_problems=ReadResult(samples, False, []))

    assert verdict.measured is False
    assert verdict.unmeasured_reason == "host_samples_damaged"


def test_host_sample_gap_of_two_intervals_keeps_trial_measured():
    samples = [_sample(0.0), _sample(20.0)]

    verdict = _judge(samples=samples, resource_problems=ReadResult(samples, False, []))

    assert verdict.measured is True


def test_host_sample_gap_of_three_intervals_keeps_trial_measured():
    samples = [_sample(0.0), _sample(30.0)]

    verdict = _judge(samples=samples, resource_problems=ReadResult(samples, False, []))

    assert verdict.measured is True


@pytest.mark.parametrize("samples", [[_sample(-28.0), _sample(3.0)], [_sample(5.0), _sample(40.0)]])
def test_host_sample_gap_touching_trial_window_is_unmeasured(samples):
    verdict = _judge(samples=samples, resource_problems=ReadResult(samples, False, []))

    assert verdict.measured is False
    assert verdict.unmeasured_reason == "host_samples_damaged"


def test_host_sample_timestamps_without_numeric_values_are_unmeasured():
    samples = [_sample(True), _sample("4")]

    verdict = _judge(samples=samples, resource_problems=ReadResult(samples, False, []))

    assert verdict.measured is False
    assert verdict.unmeasured_reason == "host_samples_damaged"


def test_host_sample_distance_of_three_intervals_from_trial_edge_is_accepted():
    started, finished = _phases(agent=(40.0, 40.0))
    facts = _facts(phase_started=started, phase_finished=finished)
    samples = [_sample(10.0)]

    verdict = _judge(facts, samples=samples, resource_problems=ReadResult(samples, False, []))

    assert verdict.measured is True


def test_host_sample_distance_beyond_three_intervals_from_trial_edge_is_unmeasured():
    started, finished = _phases(agent=(40.0, 40.0))
    facts = _facts(phase_started=started, phase_finished=finished)
    samples = [_sample(0.0)]

    verdict = _judge(facts, samples=samples, resource_problems=ReadResult(samples, False, []))

    assert verdict.measured is False
    assert verdict.unmeasured_reason == "host_samples_damaged"


def test_host_sample_damage_span_ending_before_trial_is_ignored():
    samples = ReadResult([_sample(0.0), _sample(2.0)], False, [2])

    verdict = _judge(samples=samples.rows, resource_problems=samples)

    assert verdict.measured is True


@pytest.mark.parametrize("times", [[0.0, 3.0], [5.0, 10.0]])
def test_host_sample_damage_span_touching_trial_boundary_is_unmeasured(times):
    samples = ReadResult([_sample(timestamp) for timestamp in times], False, [2])

    verdict = _judge(samples=samples.rows, resource_problems=samples)

    assert verdict.measured is False
    assert verdict.unmeasured_reason == "host_samples_damaged"


def test_host_sample_damage_during_verifier_is_unmeasured():
    started, finished = _phases(verifier=(6.0, 8.0))
    facts = _facts(phase_started=started, phase_finished=finished)
    samples = ReadResult([_sample(6.0), _sample(8.0)], False, [2])

    verdict = _judge(facts, samples=samples.rows, resource_problems=samples)

    assert verdict.measured is False
    assert verdict.unmeasured_reason == "host_samples_damaged"


def test_missing_host_samples_make_trial_unmeasured():
    verdict = _judge(samples=[], resource_problems=ReadResult([], False, []))

    assert verdict.measured is False
    assert verdict.unmeasured_reason == "host_samples_damaged"


def test_host_breach_wins_over_damaged_host_sample_line():
    samples = ReadResult([_sample(4.0, mem_available_mb=100)], False, [2])

    verdict = judge_trial(
        _facts(),
        [],
        ReadResult([], False, []),
        samples.rows,
        DEFAULT_LIMITS,
        (),
        ReadResult([], False, []),
        ReadResult([], False, []),
        resource_problems=samples,
        sampler_interval_s=10,
    )

    _assert_invalid(verdict, "host_fault", "host")


def test_agent_phase_failure_without_requests_is_valid_failure():
    verdict = _judge(
        _facts(reward=0.0, exception_type="AgentTimeoutError", harbor_reported_tokens=0),
        rows=_requests([]),
    )

    assert verdict.valid
    assert set(verdict.labels) == {"timed_out", "no_requests"}
    assert verdict.measured


def _telemetry_read(tmp_path, name, rows, malformed_line):
    path = tmp_path / name
    lines = [json.dumps(row, sort_keys=True) for row in rows]
    lines.insert(malformed_line - 1, '{"t":')
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return read_rows(path)


def _memory_sample(t, trial=TRIAL):
    return {
        "type": "sample",
        "t": t,
        "trial": trial,
        "location": "harbor-job",
        "container": "container-a",
        "used_mb": 42.5,
        "limit_mb": 128,
        "reader": "docker_stats",
    }


def _docker_oom_event(t, trial=TRIAL):
    return {
        "Type": "container",
        "Action": "oom",
        "Actor": {"Attributes": {"com.docker.compose.project": f"{trial.lower()}__env"}},
        "time": int(t),
        "timeNano": int(t * 1_000_000_000),
    }


def test_memory_watch_damage_overlapping_trial_window_makes_trial_unmeasured(tmp_path):
    memory = _telemetry_read(
        tmp_path, "memory-watch.jsonl", [_memory_sample(2), _memory_sample(6)], 2
    )

    verdict = _judge(memory_watch_events=memory)

    assert verdict.measured is False
    assert verdict.unmeasured_reason == "telemetry_damaged"


def test_half_written_last_memory_watch_row_keeps_trial_measured(tmp_path):
    path = tmp_path / "memory-watch.jsonl"
    complete = json.dumps(_memory_sample(2), sort_keys=True) + "\n"
    in_flight = json.dumps(_memory_sample(6), sort_keys=True)[:20]
    path.write_text(complete + in_flight, encoding="utf-8")

    verdict = _judge(memory_watch_events=read_rows(path))

    assert verdict.measured is True


def test_memory_watch_damage_outside_trial_window_keeps_trial_measured(tmp_path):
    memory = _telemetry_read(
        tmp_path, "memory-watch.jsonl", [_memory_sample(0), _memory_sample(2)], 2
    )

    verdict = _judge(memory_watch_events=memory)

    assert verdict.measured is True


def test_oom_event_damage_overlapping_trial_window_makes_trial_unmeasured(tmp_path):
    oom = _telemetry_read(
        tmp_path,
        "docker-oom-events.jsonl",
        [_docker_oom_event(2), _docker_oom_event(6)],
        2,
    )
    resources = [_sample(0), _sample(10)]

    verdict = judge_trial(
        _facts(),
        _clean_ledger(),
        ReadResult(_clean_ledger(), False, []),
        resources,
        DEFAULT_LIMITS,
        (),
        oom,
        ReadResult([], False, []),
        resource_problems=ReadResult(resources, False, []),
        sampler_interval_s=10,
    )

    assert verdict.measured is False
    assert verdict.unmeasured_reason == "telemetry_damaged"


@pytest.mark.parametrize(
    ("times", "bad_line"),
    [([0, 3], 2), ([5, 10], 2)],
)
def test_memory_watch_damage_touching_trial_window_is_unmeasured(tmp_path, times, bad_line):
    memory = _telemetry_read(
        tmp_path,
        "memory-watch.jsonl",
        [_memory_sample(timestamp) for timestamp in times],
        bad_line,
    )

    verdict = _judge(memory_watch_events=memory)

    assert verdict.measured is False
    assert verdict.unmeasured_reason == "telemetry_damaged"


def test_memory_watch_damage_during_verifier_is_unmeasured(tmp_path):
    started, finished = _phases(verifier=(6.0, 8.0))
    facts = _facts(phase_started=started, phase_finished=finished)
    memory = _telemetry_read(
        tmp_path,
        "memory-watch.jsonl",
        [_memory_sample(6), _memory_sample(8)],
        2,
    )

    verdict = _judge(facts, memory_watch_events=memory)

    assert verdict.measured is False
    assert verdict.unmeasured_reason == "telemetry_damaged"


def test_oom_event_damage_outside_trial_window_keeps_trial_measured(tmp_path):
    oom = _telemetry_read(
        tmp_path,
        "docker-oom-events.jsonl",
        [_docker_oom_event(0), _docker_oom_event(2)],
        2,
    )
    resources = [_sample(0), _sample(10)]

    verdict = judge_trial(
        _facts(),
        _clean_ledger(),
        ReadResult(_clean_ledger(), False, []),
        resources,
        DEFAULT_LIMITS,
        (),
        oom,
        ReadResult([], False, []),
        resource_problems=ReadResult(resources, False, []),
        sampler_interval_s=10,
    )

    assert verdict.measured is True


def test_watchdog_kill_wins_over_damaged_memory_watch_line(tmp_path):
    memory = _telemetry_read(
        tmp_path, "memory-watch.jsonl", [_memory_sample(2), _memory_sample(6)], 2
    )
    memory.rows.append({**_memory_sample(4), "type": "kill"})

    verdict = _judge(memory_watch_events=memory)

    assert verdict.labels[0] == "oom_killed"
    assert verdict.oom_source == "watchdog"


def test_exception_before_verifier_is_labelled_as_agent_exit():
    started, finished = _phases(verifier=(6.0, 7.0))
    verdict = _judge(
        _facts(
            reward=0.0,
            exception_type="NonZeroAgentExitCodeError",
            exception_at=5.0,
            phase_started=started,
            phase_finished=finished,
        )
    )

    assert verdict.valid
    assert "exit_nonzero" in verdict.labels
    assert "verifier_failed" not in verdict.labels


def test_environment_setup_failure_does_not_require_agent_phase_times():
    started, finished = _phases(agent=(None, None))
    facts = _facts(
        exception_type="EnvironmentSetupError",
        exception_at=1.2,
        phase_started=started,
        phase_finished=finished,
    )

    verdict = _judge(facts, rows=_clean_ledger())

    assert verdict.reason == "environment_failed"
    assert verdict.measured is True


def test_agent_setup_failure_does_not_require_agent_phase_times():
    started, finished = _phases(agent=(None, None))
    facts = _facts(
        exception_type="HarnessSetupError",
        exception_at=2.2,
        phase_started=started,
        phase_finished=finished,
    )

    verdict = _judge(facts, rows=_clean_ledger(), earlier=("harness_setup_failed",))

    assert verdict.valid is True
    assert verdict.measured is True


def test_oom_events_are_not_checked_without_a_complete_trial_window():
    started, finished = _phases(agent=(3.0, None))
    facts = _facts(phase_started=started, phase_finished=finished)
    event = _docker_oom_event(4.0)

    verdict = _judge(facts, rows=_clean_ledger(), oom_events=[event])

    assert verdict.valid is True
    assert verdict.oom_source is None


def test_oom_search_continues_after_non_object_event():
    event = _docker_oom_event(4.0)

    verdict = _judge(rows=_clean_ledger(), oom_events=[None, event])

    assert verdict.oom_source == "kernel"


@pytest.mark.parametrize("event", [{}, {"Actor": {}}])
def test_oom_search_ignores_incomplete_event_shapes(event):
    verdict = _judge(rows=_clean_ledger(), oom_events=[event, _docker_oom_event(4.0)])

    assert verdict.oom_source == "kernel"


@pytest.mark.parametrize("timestamp", [3.0, 5.0])
def test_kernel_oom_at_trial_window_boundary_is_counted(timestamp):
    verdict = _judge(rows=_clean_ledger(), oom_events=[_docker_oom_event(timestamp)])

    assert verdict.oom_source == "kernel"


def test_watchdog_event_for_another_row_type_does_not_count_as_oom():
    sample = {**_memory_sample(4.0), "trial": TRIAL}

    verdict = _judge(rows=_clean_ledger(), memory_watch_events=[sample])

    assert verdict.oom_source is None


def test_watchdog_event_for_matching_trial_with_non_numeric_time_is_ignored():
    event = {"type": "kill", "trial": TRIAL, "t": "bad"}

    verdict = _judge(rows=_clean_ledger(), memory_watch_events=[event])

    assert verdict.oom_source is None


@pytest.mark.parametrize("timestamp", [3.0, 5.0])
def test_watchdog_kill_at_trial_window_boundary_is_counted(timestamp):
    event = {"type": "kill", "trial": TRIAL, "t": timestamp}

    verdict = _judge(rows=_clean_ledger(), memory_watch_events=[event])

    assert verdict.oom_source == "watchdog"


def test_invalid_verdict_preserves_measurement_reason_and_empty_oom_source():
    verdict = _judge(_facts(exception_type="CancelledError"), rows=_clean_ledger())

    assert verdict.valid is False
    assert verdict.reason == "cancelled"
    assert verdict.measured is True
    assert verdict.unmeasured_reason is None
    assert verdict.oom_source is None


def test_invalid_verdict_preserves_unmeasured_reason():
    rows = _clean_ledger()
    facts = _facts(has_result=False)

    verdict = judge_trial(
        facts,
        rows,
        ReadResult(rows, False, [3]),
        [_sample(0.0), _sample(10.0)],
        DEFAULT_LIMITS,
        (),
        ReadResult([], False, []),
        ReadResult([], False, []),
        resource_problems=ReadResult([_sample(0.0), _sample(10.0)], False, []),
        sampler_interval_s=10,
    )

    assert verdict.valid is False
    assert verdict.reason == "never_finished"
    assert verdict.measured is False
    assert verdict.unmeasured_reason == "ledger_damaged"


def test_repeated_setup_failure_keeps_boolean_measurement_value():
    started, finished = _phases(agent=(None, None))
    facts = _facts(
        reward=0.0,
        exception_type="HarnessSetupError",
        phase_started=started,
        phase_finished=finished,
        harbor_reported_tokens=0,
    )

    verdict = _judge(facts, rows=[], earlier=("harness_setup_failed",))

    assert verdict.measured is False


def test_repeated_setup_failure_does_not_add_oom_label():
    facts = _facts(
        exception_type="HarnessSetupError",
        exception_at=2.6,
        phase_started={**_phases()[0], "agent": 3.0},
        phase_finished=_phases()[1],
    )

    verdict = _judge(
        facts,
        rows=_clean_ledger(),
        earlier=("harness_setup_failed",),
        oom_events=[_docker_oom_event(4.0)],
    )

    assert "oom_killed" not in verdict.labels


def test_repeated_setup_failure_counts_as_failure():
    started, finished = _phases(agent=(None, None))
    facts = _facts(
        reward=0.0,
        exception_type="HarnessSetupError",
        phase_started=started,
        phase_finished=finished,
        harbor_reported_tokens=0,
    )

    verdict = _judge(facts, rows=[], earlier=("harness_setup_failed",))

    assert verdict.valid
    assert "harness_could_not_start" in verdict.labels
    assert not verdict.measured
    assert verdict.unmeasured_reason == "no_open_row"


def test_repeated_setup_failure_is_valid_without_a_reward():
    started, finished = _phases(agent=(None, None))
    facts = _facts(
        reward=None,
        exception_type="HarnessSetupError",
        phase_started=started,
        phase_finished=finished,
        harbor_reported_tokens=0,
    )

    verdict = _judge(facts, rows=[], earlier=("harness_setup_failed",))

    assert verdict.valid
    assert "harness_could_not_start" in verdict.labels


def test_missing_reward_outside_verifier_is_no_verdict():
    verdict = _judge(_facts(reward=None, exception_type="AgentTimeoutError"))

    _assert_invalid(verdict, "no_verdict", "harbor")


def test_missing_result_is_never_finished():
    verdict = _judge(_facts(has_result=False, reward=None))

    _assert_invalid(verdict, "never_finished", "harbor")


def test_cancelled_exception_is_cancelled():
    verdict = _judge(_facts(exception_type="CancelledError"))

    _assert_invalid(verdict, "cancelled", "harbor")


def test_rule_order_cancelled_outranks_upstream_errors():
    verdict = _judge(
        _facts(exception_type="CancelledError"),
        rows=_requests(["ok", "server_error"]),
    )

    _assert_invalid(verdict, "cancelled", "harbor")


def test_environment_setup_failure_is_environment_failed():
    started, finished = _phases(setup=(None, None), agent=(None, None))
    facts = _facts(
        exception_type="EnvironmentError",
        phase_started=started,
        phase_finished=finished,
    )

    verdict = _judge(facts)

    _assert_invalid(verdict, "environment_failed", "harbor")


def test_first_harness_setup_failure_is_invalid():
    started, finished = _phases(agent=(None, None))
    facts = _facts(
        exception_type="HarnessSetupError",
        phase_started=started,
        phase_finished=finished,
    )

    verdict = _judge(facts, rows=[])

    _assert_invalid(verdict, "harness_setup_failed", "harbor")


def test_host_breach_outside_agent_window_is_ignored():
    samples = [_sample(2.0, mem_available_mb=100)]

    verdict = _judge(samples=samples)

    assert verdict.valid


def test_host_breach_after_agent_finish_is_ignored_without_verifier():
    samples = [_sample(6.0, mem_available_mb=100)]

    verdict = _judge(samples=samples)

    assert verdict.valid


def test_docker_not_ok_in_agent_window_invalidates_trial():
    samples = [_sample(4.0, docker_ok=False)]

    verdict = _judge(samples=samples)

    _assert_invalid(verdict, "host_fault", "host")


def test_host_breach_does_not_apply_without_agent_start():
    started, finished = _phases(agent=(None, None), verifier=(None, None))
    facts = _facts(phase_started=started, phase_finished=finished)
    samples = [_sample(4.0, mem_available_mb=100)]

    verdict = _judge(facts, samples=samples)

    assert verdict.valid


def test_missing_reward_after_verifier_failure_is_valid():
    started, finished = _phases(verifier=(6.0, None))
    facts = _facts(
        reward=None,
        exception_type="VerifierError",
        phase_started=started,
        phase_finished=finished,
    )

    verdict = _judge(facts)

    assert verdict.valid
    assert "verifier_failed" in verdict.labels


def test_labels_are_empty_on_an_invalid_verdict():
    rows = [_open_row(1), _close_row(2), _refused(3, "closed")]

    verdict = _judge(_facts(exception_type="CancelledError"), rows=rows)

    assert verdict.labels == ()


def test_refused_after_close_label():
    rows = [_open_row(1), _close_row(2), _refused(3, "closed")]

    verdict = _judge(_facts(harbor_reported_tokens=0), rows=rows)

    assert "refused_after_close" in verdict.labels


def test_refused_not_open_does_not_add_label():
    rows = [_open_row(1), _close_row(2), _refused(3, "not_open")]

    verdict = _judge(_facts(harbor_reported_tokens=0), rows=rows)

    assert "refused_after_close" not in verdict.labels


def test_timed_out_label():
    verdict = _judge(_facts(reward=0.0, exception_type="AgentTimeoutError"))

    assert verdict.valid
    assert "timed_out" in verdict.labels
    assert "exit_nonzero" not in verdict.labels


def test_verifier_failure_is_labelled():
    started, finished = _phases(verifier=(6.0, None))
    facts = _facts(
        reward=0.0,
        exception_type="VerifierError",
        phase_started=started,
        phase_finished=finished,
    )

    verdict = _judge(facts)

    assert verdict.valid
    assert set(verdict.labels) == {"verifier_failed"}


def test_no_requests_label():
    verdict = _judge(
        _facts(harbor_reported_tokens=0),
        rows=[_open_row(1), _close_row(2)],
    )

    assert set(verdict.labels) == {"no_requests"}


def test_sequence_gap_between_open_and_close_marks_trial_unmeasured():
    rows = [_open_row(1), _request_start(2, "r1"), _request_end(4, "r1"), _close_row(5)]

    verdict = _judge(rows=rows)

    assert not verdict.measured
    assert verdict.unmeasured_reason == "ledger_damaged"


def test_sequence_gap_after_close_is_ignored():
    rows = _clean_ledger()
    rows.append(_open_row(6, trial="other"))
    rows.append(_close_row(7, trial="other"))

    verdict = _judge(rows=rows)

    assert verdict.measured


def test_sequence_gap_after_open_without_close_is_unmeasured():
    rows = [_open_row(1), _request_start(2, "r1"), _request_end(4, "r1")]

    verdict = _judge(rows=rows)

    assert not verdict.measured
    assert verdict.unmeasured_reason == "ledger_damaged"


def test_duplicate_sequence_inside_open_and_close_marks_trial_unmeasured():
    rows = [
        _open_row(1),
        _request_start(2, "r1"),
        _request_end(3, "r1"),
        _request_start(3, "r2"),
        _request_end(4, "r2"),
        _close_row(5),
    ]

    verdict = _judge(rows=rows)

    assert not verdict.measured
    assert verdict.unmeasured_reason == "ledger_damaged"


def test_reversed_sequence_inside_open_and_close_marks_trial_unmeasured():
    rows = [
        _open_row(1),
        _request_start(2, "r1"),
        _request_end(3, "r1"),
        _request_start(5, "r2"),
        _request_end(4, "r2"),
        _close_row(6),
    ]

    verdict = _judge(rows=rows)

    assert not verdict.measured
    assert verdict.unmeasured_reason == "ledger_damaged"


def test_backward_sequence_reset_inside_trial_marks_it_unmeasured():
    rows = [
        _open_row(5),
        _request_start(6, "r1"),
        _request_end(5, "r1"),
        _request_start(6, "r2"),
        _request_end(7, "r2"),
        _close_row(8),
    ]

    verdict = _judge(rows=rows)

    assert not verdict.measured
    assert verdict.unmeasured_reason == "ledger_damaged"


def test_backward_sequence_reset_after_close_is_ignored():
    rows = _clean_ledger()
    rows.extend(
        [
            _open_row(5, trial="other"),
            _request_start(6, "o1", trial="other"),
            _request_end(5, "o1", trial="other"),
            _close_row(7, trial="other"),
        ]
    )

    verdict = _judge(rows=rows)

    assert verdict.measured


def test_duplicated_close_sequence_marks_trial_unmeasured():
    rows = [
        _open_row(1),
        _request_start(2, "r1"),
        _request_end(3, "r1"),
        _request_start(4, "r2"),
        _request_end(5, "r2"),
        _close_row(5),
    ]

    verdict = _judge(rows=rows)

    assert not verdict.measured
    assert verdict.unmeasured_reason == "ledger_damaged"


def test_duplicate_sequence_outside_window_is_ignored():
    rows = _clean_ledger()
    rows.extend(
        [
            _open_row(5, trial="other"),
            _request_start(6, "o1", trial="other"),
            _request_end(6, "o1", trial="other"),
            _close_row(7, trial="other"),
        ]
    )

    verdict = _judge(rows=rows)

    assert verdict.measured


def test_reversed_sequence_outside_window_is_ignored():
    rows = _clean_ledger()
    rows.extend(
        [
            _open_row(5, trial="other"),
            _request_start(7, "o1", trial="other"),
            _request_end(6, "o1", trial="other"),
            _close_row(8, trial="other"),
        ]
    )

    verdict = _judge(rows=rows)

    assert verdict.measured


def test_request_start_without_end_without_close_is_measured():
    rows = [_open_row(1), _request_start(2, "r1")]

    verdict = _judge(_facts(has_result=False), rows=rows)

    assert verdict.measured


def test_requests_without_open_row_are_unmeasured():
    rows = [_request_start(1, "r1"), _request_end(2, "r1")]

    verdict = _judge(rows=rows)

    assert not verdict.measured
    assert verdict.unmeasured_reason == "no_open_row"


def test_result_without_open_row_is_unmeasured():
    verdict = _judge(_facts(reward=1.0, harbor_reported_tokens=0), rows=[])

    assert not verdict.measured
    assert verdict.unmeasured_reason == "no_open_row"


def test_open_trial_without_requests_is_measured():
    verdict = _judge(_facts(harbor_reported_tokens=0), rows=[_open_row(1), _close_row(2)])

    assert verdict.measured
    assert verdict.labels == ("no_requests",)


def test_result_without_agent_phase_window_is_unmeasured():
    phase_started, phase_finished = _phases(agent=(None, 5.0))
    verdict = _judge(
        _facts(phase_started=phase_started, phase_finished=phase_finished),
        rows=[_open_row(1), _close_row(2)],
    )

    assert not verdict.measured
    assert verdict.unmeasured_reason == "phase_times_missing"


def test_missing_agent_start_with_malformed_memory_watch_line_is_phase_times_missing(tmp_path):
    phase_started, phase_finished = _phases(agent=(None, 5.0))
    memory = _telemetry_read(
        tmp_path, "memory-watch.jsonl", [_memory_sample(2), _memory_sample(6)], 2
    )

    verdict = _judge(
        _facts(phase_started=phase_started, phase_finished=phase_finished),
        rows=[_open_row(1), _close_row(2)],
        memory_watch_events=memory,
    )

    assert not verdict.measured
    assert verdict.unmeasured_reason == "phase_times_missing"


@pytest.mark.parametrize("field", ["prompt_tokens", "completion_tokens", "cached_read_tokens"])
def test_string_token_value_marks_trial_unmeasured(field):
    rows = _clean_ledger()
    rows[-2][field] = "12"

    verdict = _judge(rows=rows)

    assert not verdict.measured
    assert verdict.unmeasured_reason == "ledger_damaged"


def test_string_duration_marks_trial_unmeasured():
    rows = _clean_ledger()
    rows[-2]["duration_s"] = "1.0"

    verdict = _judge(rows=rows)

    assert not verdict.measured
    assert verdict.unmeasured_reason == "ledger_damaged"


def test_string_sequence_marks_trial_unmeasured():
    rows = _clean_ledger()
    rows[-2]["seq"] = "3"

    verdict = _judge(rows=rows)

    assert not verdict.measured
    assert verdict.unmeasured_reason == "ledger_damaged"


@pytest.mark.parametrize("sequence", [0, -1, True, 1.0, None])
def test_non_positive_or_non_integer_sequence_marks_trial_unmeasured(sequence):
    rows = _clean_ledger()
    rows[-2]["seq"] = sequence

    verdict = _judge(rows=rows)

    assert verdict.measured is False
    assert verdict.unmeasured_reason == "ledger_damaged"


def test_missing_numeric_field_marks_trial_unmeasured():
    rows = _clean_ledger()
    rows[-2].pop("duration_s")

    verdict = _judge(rows=rows)

    assert not verdict.measured
    assert verdict.unmeasured_reason == "ledger_damaged"


def test_unknown_upstream_status_marks_trial_unmeasured():
    rows = _clean_ledger()
    rows[-2]["upstream_status"] = "unknown"

    verdict = _judge(rows=rows)

    assert not verdict.measured
    assert verdict.unmeasured_reason == "ledger_damaged"


def test_nonfinite_duration_marks_trial_unmeasured():
    rows = _clean_ledger()
    rows[-2]["duration_s"] = float("inf")

    verdict = _judge(rows=rows)

    assert not verdict.measured
    assert verdict.unmeasured_reason == "ledger_damaged"


def test_unhashable_request_id_marks_trial_unmeasured():
    rows = _clean_ledger()
    rows[1]["request_id"] = ["not", "hashable"]

    verdict = _judge(rows=rows)

    assert not verdict.measured
    assert verdict.unmeasured_reason == "ledger_damaged"


def test_globally_malformed_row_inside_open_interval_marks_trial_unmeasured():
    rows = _clean_ledger()
    rows.insert(2, {"type": "request_end"})

    verdict = _judge(rows=rows)

    assert not verdict.measured
    assert verdict.unmeasured_reason == "ledger_damaged"


def test_ok_request_with_unknown_usage_is_unmeasured():
    rows = _clean_ledger()
    rows[-2]["completion_tokens"] = None

    verdict = _judge(rows=rows)

    assert verdict.valid
    assert not verdict.measured
    assert verdict.unmeasured_reason == "usage_incomplete"


def test_malformed_row_is_skipped_for_status_classification():
    rows = _ledger_from_ends([{"status": "ok"}, {"status": "server_error"}])
    rows[-2]["completion_tokens"] = "12"

    verdict = _judge(rows=rows)

    assert verdict.valid
    assert not verdict.measured
    assert verdict.unmeasured_reason == "ledger_damaged"


def test_reward_does_not_change_an_invalid_verdict():
    started, finished = _phases(setup=(None, None), agent=(None, None))
    setup_started, setup_finished = _phases(agent=(None, None))
    fixtures = [
        ("never_finished", _facts(has_result=False, reward=0.0), _clean_ledger(), []),
        ("cancelled", _facts(exception_type="CancelledError"), _clean_ledger(), []),
        (
            "environment_failed",
            _facts(
                exception_type="EnvironmentError",
                phase_started=started,
                phase_finished=finished,
            ),
            _clean_ledger(),
            [],
        ),
        (
            "harness_setup_failed",
            _facts(
                exception_type="HarnessSetupError",
                phase_started=setup_started,
                phase_finished=setup_finished,
            ),
            [],
            [],
        ),
        ("host_fault", _facts(), _clean_ledger(), [_sample(4.0, mem_available_mb=100)]),
        ("upstream_terminal_error", _facts(), _requests(["ok", "server_error"]), []),
        ("upstream_errors", _facts(), _requests(["server_error"] * 3 + ["ok"] * 97), []),
        (
            "upstream_slow",
            _facts(),
            _ledger_from_ends([{"completion_tokens": 10, "duration_s": 10.0}]),
            [],
        ),
    ]

    for name, facts, rows, samples in fixtures:
        at_one = _judge(replace(facts, reward=1.0), rows=rows, samples=samples)
        at_zero = _judge(replace(facts, reward=0.0), rows=rows, samples=samples)
        assert at_one == at_zero, name
        assert not at_one.valid, name
