import os
from pathlib import Path

import pytest

from harbor_research_runner.records.limits import Limits
from harbor_research_runner.records.proxy_ledger import RequestEnd
from harbor_research_runner.trial_validity.upstream import upstream_reason
from tests.support.old_evidence import old_null_end_count, old_request_rows, old_trial_rows

EVIDENCE = Path("evidence/ollama-beam-119-vs-21-k3-i5-20261106T000709307600Z")
UPSTREAM_CHUNKS = (
    (0, {"compile-compcert__UpgyXXG": "upstream_terminal_error"}, 1, 0),
    (100, {}, 0, 0),
    (200, {}, 2, 2),
    (300, {}, 0, 0),
    (400, {"multi-source-data-merger__cMSorkz": "upstream_terminal_error"}, 2, 1),
    (500, {}, 0, 0),
    (600, {}, 2, 2),
    (700, {}, 0, 0),
)

LIMITS = Limits.from_dict({}, repetitions=1, verifier_timeout_multiplier=1.5)


@pytest.fixture(scope="module")
def evidence():
    evidence_dir = Path(os.environ["HRR_OLD_REPO"]) / EVIDENCE
    return (
        old_trial_rows(evidence_dir),
        old_request_rows(evidence_dir),
        old_null_end_count(evidence_dir),
    )


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


@pytest.mark.replay
@pytest.mark.skipif(not os.environ.get("HRR_OLD_REPO"), reason="HRR_OLD_REPO is unset")
@pytest.mark.parametrize(
    ("start", "expected_invalid", "expected_single_error", "expected_valid_single_error"),
    UPSTREAM_CHUNKS,
    ids=[f"trials-{start}-{min(start + 99, 745)}" for start, *_ in UPSTREAM_CHUNKS],
)
def test_beam_evidence_upstream_verdicts(
    evidence, start, expected_invalid, expected_single_error, expected_valid_single_error
):
    _, request_rows, _ = evidence
    limits = Limits.from_dict({}, repetitions=1, verifier_timeout_multiplier=1.0)
    trials = sorted(request_rows)
    chunk = trials[start : start + 100]
    verdicts = {trial: upstream_reason(request_rows[trial], limits) for trial in chunk}
    invalid = {trial: reason for trial, reason in verdicts.items() if reason is not None}
    single_error = {
        trial
        for trial in chunk
        if sum(row["upstream_status"] != "ok" for row in request_rows[trial]) == 1
    }

    assert len(trials) == 746
    assert sum(item[2] for item in UPSTREAM_CHUNKS) == 7
    assert sum(item[3] for item in UPSTREAM_CHUNKS) == 5
    assert invalid == expected_invalid
    assert len(single_error) == expected_single_error
    assert sum(verdicts[trial] is None for trial in single_error) == expected_valid_single_error
