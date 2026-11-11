from harbor_research_runner.records.limits import Limits
from harbor_research_runner.records.proxy_ledger import RequestEnd
from harbor_research_runner.trial_validity.upstream import upstream_reason

LIMITS = Limits.from_dict({}, repetitions=1, verifier_timeout_multiplier=1.5)


def _end(seq, status="ok", completion_tokens=100, duration_s=1.0):
    return RequestEnd(
        seq,
        float(seq),
        "trial",
        f"r{seq}",
        200,
        status,
        200,
        "model",
        100,
        completion_tokens,
        0,
        100,
        0.1,
        0.2,
        duration_s,
        0,
        None,
    ).to_row()


def _ends(statuses):
    return [_end(index + 1, status) for index, status in enumerate(statuses)]


def test_no_request_end_rows_returns_none():
    assert upstream_reason([], LIMITS) is None


def test_last_error_returns_terminal_error():
    assert upstream_reason(_ends(["ok", "server_error"]), LIMITS) == "upstream_terminal_error"


def test_error_before_a_clean_last_request_is_not_terminal():
    assert upstream_reason(_ends(["server_error", "ok"]), LIMITS) is None


def test_two_errors_in_100_requests_are_tolerated():
    assert upstream_reason(_ends(["server_error"] * 2 + ["ok"] * 98), LIMITS) is None


def test_three_errors_in_100_requests_exceed_tolerance():
    assert upstream_reason(_ends(["server_error"] * 3 + ["ok"] * 97), LIMITS) == "upstream_errors"


def test_two_errors_in_20_requests_exceed_tolerance():
    assert upstream_reason(_ends(["server_error"] * 2 + ["ok"] * 18), LIMITS) == "upstream_errors"


def test_client_errors_do_not_count_as_upstream_errors():
    assert upstream_reason(_ends(["client_error"] * 5 + ["ok"] * 5), LIMITS) is None


def test_client_disconnects_are_excluded_from_the_error_denominator():
    rows = _ends(["server_error"] * 5 + ["ok"] * 95 + ["client_disconnect"] * 300)

    assert upstream_reason(rows, LIMITS) == "upstream_errors"


def test_slow_output_rate_returns_slow():
    rows = [_end(1, completion_tokens=10, duration_s=10.0)]

    assert upstream_reason(rows, LIMITS) == "upstream_slow"


def test_output_rate_at_the_floor_is_not_slow():
    rows = [_end(1, completion_tokens=20, duration_s=10.0)]

    assert upstream_reason(rows, LIMITS) is None


def test_zero_completion_tokens_is_not_slow():
    rows = [_end(1, completion_tokens=0, duration_s=10.0)]

    assert upstream_reason(rows, LIMITS) is None


def test_zero_duration_rows_are_excluded_from_output_rate():
    rows = [
        _end(1, completion_tokens=10, duration_s=10.0),
        _end(2, completion_tokens=1000, duration_s=0.0),
    ]

    assert upstream_reason(rows, LIMITS) == "upstream_slow"


def test_error_rows_do_not_count_toward_output_rate():
    rows = [
        _end(1, "server_error", completion_tokens=0, duration_s=100.0),
        _end(2, "ok", completion_tokens=100, duration_s=1.0),
    ]

    assert upstream_reason(rows, LIMITS) is None


def test_malformed_rows_are_skipped():
    rows = _ends(["ok", "server_error"])
    rows[-1]["completion_tokens"] = "12"

    assert upstream_reason(rows, LIMITS) is None


def test_malformed_last_error_does_not_become_terminal():
    rows = _ends(["ok", "server_error"])
    rows[-1]["duration_s"] = "1.0"

    assert upstream_reason(rows, LIMITS) is None
