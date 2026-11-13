from harbor_research_runner.records.limits import Limits
from harbor_research_runner.trial_validity.run_health import (
    RunSnapshot,
    inactivity_limit_s,
    should_halt,
)

NOW = 10_000.0
TRIAL = "task__abc123"
DEFAULT_LIMITS = Limits.from_dict({}, repetitions=1, verifier_timeout_multiplier=1.0)


def _request_end(seq, t, status="ok"):
    return {
        "type": "request_end",
        "seq": seq,
        "t": t,
        "trial": TRIAL,
        "request_id": f"r{seq}",
        "http_status": 200,
        "upstream_status": status,
        "upstream_http_status": 200,
        "served_model": "model",
        "prompt_tokens": 100,
        "completion_tokens": 100,
        "cached_read_tokens": 0,
        "completion_chars": 100,
        "first_chunk_s": 0.1,
        "first_content_s": 0.2,
        "duration_s": 1.0,
        "failovers": 0,
        "error_excerpt": None,
    }


def _open(seq, t):
    return {
        "type": "open",
        "seq": seq,
        "t": t,
        "trial": TRIAL,
        "task": "task",
        "arm": "oc-1",
    }


def _verdict(valid=True, instrument=None, labels=(), reason=None):
    return {
        "type": "trial_verdict",
        "t": NOW,
        "trial": TRIAL,
        "task": "task",
        "arm": "oc-1",
        "valid": valid,
        "reason": reason,
        "instrument": instrument,
        "labels": list(labels),
        "measured": True,
        "unmeasured_reason": None,
    }


def _proxy_invalid():
    return _verdict(valid=False, instrument="proxy", reason="upstream_errors")


def _host_invalid():
    return _verdict(valid=False, instrument="host", reason="host_fault")


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


def _breach(t):
    return _sample(t, mem_available_mb=1024)


def _snapshot(**overrides):
    values = {
        "now": NOW,
        "ledger_rows_since_pass_start": [],
        "verdicts": [],
        "open_trials": 0,
        "proxy_alive": True,
        "oom_watch_alive": True,
        "memory_watch_alive": True,
        "resource_rows": [_sample(NOW)],
        "last_activity_t": NOW,
        "longest_agent_budget_s": 3600.0,
        "longest_verifier_budget_s": 900.0,
        "harbor_timeout_multiplier": 1.0,
    }
    values.update(overrides)
    return RunSnapshot(**values)


def test_consecutive_clean_timeouts_do_not_halt():
    verdicts = [_verdict(labels=("timed_out",)) for _ in range(10)]

    assert should_halt(_snapshot(verdicts=verdicts), DEFAULT_LIMITS) is None


def test_dead_memory_watch_halts():
    assert should_halt(_snapshot(memory_watch_alive=False), DEFAULT_LIMITS) == (
        "memory_watch_dead",
        "memory watcher process exited",
    )


def test_healthy_snapshot_does_not_halt():
    rows = [_open(1, NOW - 120), _request_end(2, NOW - 10)]

    assert should_halt(_snapshot(ledger_rows_since_pass_start=rows), DEFAULT_LIMITS) is None


def test_dead_oom_watcher_halts():
    assert should_halt(_snapshot(oom_watch_alive=False), DEFAULT_LIMITS) == (
        "oom_watch_dead",
        "OOM watcher process exited",
    )


def test_upstream_invalid_streak_halts():
    verdicts = [_proxy_invalid() for _ in range(5)]

    result = should_halt(_snapshot(verdicts=verdicts), DEFAULT_LIMITS)

    assert result is not None
    assert result[0] == "upstream_invalid_streak"


def test_four_upstream_invalid_verdicts_then_a_valid_one_do_not_halt():
    verdicts = [_proxy_invalid() for _ in range(4)] + [_verdict()]

    assert should_halt(_snapshot(verdicts=verdicts), DEFAULT_LIMITS) is None


def test_a_host_invalid_verdict_breaks_the_upstream_streak():
    verdicts = [_proxy_invalid() for _ in range(4)] + [_host_invalid()]

    assert should_halt(_snapshot(verdicts=verdicts), DEFAULT_LIMITS) is None


def test_upstream_invalid_streak_halts_while_billing_is_fresh():
    rows = [_open(1, NOW - 120), _request_end(2, NOW - 10)]
    verdicts = [_proxy_invalid() for _ in range(5)]

    result = should_halt(
        _snapshot(ledger_rows_since_pass_start=rows, verdicts=verdicts), DEFAULT_LIMITS
    )

    assert result is not None
    assert result[0] == "upstream_invalid_streak"


def test_quota_error_halts_run_health():
    rows = [_open(1, NOW - 120), _request_end(2, NOW - 10, status="quota_exhausted")]

    result = should_halt(_snapshot(ledger_rows_since_pass_start=rows), DEFAULT_LIMITS)

    assert result is not None
    assert result[0] == "quota_exhausted"


def test_quota_exhausted_halts_before_an_invalid_streak():
    rows = [_request_end(1, NOW - 10, status="quota_exhausted")]
    verdicts = [_proxy_invalid() for _ in range(5)]

    result = should_halt(
        _snapshot(ledger_rows_since_pass_start=rows, verdicts=verdicts), DEFAULT_LIMITS
    )

    assert result is not None
    assert result[0] == "quota_exhausted"


def test_zero_billed_window_halts_without_an_ok_request():
    rows = [_open(1, NOW - 1000)]

    result = should_halt(
        _snapshot(ledger_rows_since_pass_start=rows, open_trials=1), DEFAULT_LIMITS
    )

    assert result is not None
    assert result[0] == "zero_billed_window"


def test_recent_ok_request_keeps_the_zero_billed_window_open():
    rows = [_open(1, NOW - 2000), _request_end(2, NOW - 100)]

    assert (
        should_halt(_snapshot(ledger_rows_since_pass_start=rows, open_trials=1), DEFAULT_LIMITS)
        is None
    )


def test_zero_billed_window_is_disabled_when_no_trial_is_open():
    rows = [_open(1, NOW - 2000)]

    assert should_halt(_snapshot(ledger_rows_since_pass_start=rows), DEFAULT_LIMITS) is None


def test_empty_pass_ledger_anchors_the_zero_billed_window_at_last_activity():
    result = should_halt(_snapshot(open_trials=1, last_activity_t=NOW - 1000), DEFAULT_LIMITS)

    assert result is not None
    assert result[0] == "zero_billed_window"


def test_proxy_dead_halts_run():
    result = should_halt(_snapshot(proxy_alive=False), DEFAULT_LIMITS)

    assert result is not None
    assert result[0] == "proxy_dead"


def test_sampler_dead_halts_when_the_latest_sample_is_older_than_forty_seconds():
    result = should_halt(_snapshot(resource_rows=[_sample(NOW - 40.5)]), DEFAULT_LIMITS)

    assert result is not None
    assert result[0] == "sampler_dead"


def test_sampler_dead_halts_without_any_resource_sample():
    result = should_halt(_snapshot(resource_rows=[]), DEFAULT_LIMITS)

    assert result is not None
    assert result[0] == "sampler_dead"


def test_sample_age_of_exactly_forty_seconds_is_still_recent():
    assert should_halt(_snapshot(resource_rows=[_sample(NOW - 40)]), DEFAULT_LIMITS) is None


def test_sustained_host_breach_halts_run():
    rows = [_breach(NOW - offset) for offset in range(130, -1, -10)]

    result = should_halt(_snapshot(resource_rows=rows), DEFAULT_LIMITS)

    assert result is not None
    assert result[0] == "host_breach"


def test_one_transient_host_breach_does_not_halt():
    rows = [_sample(NOW - offset) for offset in range(130, 0, -10)] + [_breach(NOW)]

    assert should_halt(_snapshot(resource_rows=rows), DEFAULT_LIMITS) is None


def test_host_breach_suffix_at_exactly_the_window_halts():
    rows = [_breach(NOW - 120), _breach(NOW)]

    result = should_halt(_snapshot(resource_rows=rows), DEFAULT_LIMITS)

    assert result is not None
    assert result[0] == "host_breach"


def test_inactivity_limit_exceeds_longest_task_budget():
    limits = Limits.from_dict({}, repetitions=1, verifier_timeout_multiplier=2.0)
    snapshot = _snapshot(
        longest_agent_budget_s=14400,
        longest_verifier_budget_s=900,
        harbor_timeout_multiplier=1.0,
    )

    assert inactivity_limit_s(snapshot, limits) == 16800


def test_inactivity_at_exactly_the_limit_does_not_halt():
    limits = Limits.from_dict({}, repetitions=1, verifier_timeout_multiplier=2.0)
    snapshot = _snapshot(
        last_activity_t=NOW - 16800,
        longest_agent_budget_s=14400,
        longest_verifier_budget_s=900,
        harbor_timeout_multiplier=1.0,
    )

    assert should_halt(snapshot, limits) is None


def test_inactivity_beyond_the_limit_halts_run():
    limits = Limits.from_dict({}, repetitions=1, verifier_timeout_multiplier=2.0)
    snapshot = _snapshot(
        last_activity_t=NOW - 16801,
        longest_agent_budget_s=14400,
        longest_verifier_budget_s=900,
        harbor_timeout_multiplier=1.0,
    )

    result = should_halt(snapshot, limits)

    assert result is not None
    assert result[0] == "inactive"
