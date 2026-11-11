import pytest

from harbor_research_runner.records.proxy_ledger import (
    COUNTS_AGAINST_TRIAL,
    UPSTREAM_STATUSES,
    Close,
    Open,
    ProxyStart,
    Refused,
    RequestEnd,
    RequestStart,
    parse_row,
    rows_for_trial,
    sequence_gaps,
)


@pytest.mark.parametrize(
    "record",
    [
        ProxyStart(1, 1.0, "v", "a" * 64, False),
        Open(2, 2.0, "trial", "task", "arm"),
        RequestStart(3, 3.0, "trial", "request", "model", True, 12, 0.25),
        RequestEnd(
            4,
            4.0,
            "trial",
            "request",
            200,
            "ok",
            200,
            "served",
            10,
            5,
            2,
            8,
            0.1,
            0.2,
            1.0,
            0,
            None,
        ),
        Refused(5, 5.0, "trial", "closed"),
        Close(6, 6.0, "trial"),
    ],
)
def test_row_types_round_trip(record):
    assert parse_row(record.to_row()) == record


def test_unknown_type_is_rejected():
    with pytest.raises(ValueError):
        parse_row({"type": "unknown"})


def test_missing_field_is_rejected():
    with pytest.raises(ValueError):
        parse_row({"type": "close", "seq": 1, "t": 1.0})


def _request_end_row():
    return RequestEnd(
        1,
        1.0,
        "trial",
        "request",
        200,
        "ok",
        200,
        "served",
        10,
        5,
        2,
        8,
        0.1,
        0.2,
        1.0,
        0,
        None,
    ).to_row()


def test_unknown_upstream_status_is_rejected():
    row = _request_end_row()
    row["upstream_status"] = "unknown"

    with pytest.raises(ValueError):
        parse_row(row)


@pytest.mark.parametrize("field", ["prompt_tokens", "completion_tokens", "cached_read_tokens"])
def test_string_token_value_is_rejected(field):
    row = _request_end_row()
    row[field] = "12"

    with pytest.raises(ValueError):
        parse_row(row)


@pytest.mark.parametrize("value", [float("inf"), float("nan")])
def test_nonfinite_duration_is_rejected(value):
    row = _request_end_row()
    row["duration_s"] = value

    with pytest.raises(ValueError):
        parse_row(row)


def test_bool_is_not_accepted_as_an_int():
    row = _request_end_row()
    row["seq"] = True

    with pytest.raises(ValueError):
        parse_row(row)


def test_nonpositive_sequence_is_rejected():
    row = _request_end_row()
    row["seq"] = 0

    with pytest.raises(ValueError):
        parse_row(row)


def test_missing_numeric_field_is_rejected():
    row = _request_end_row()
    row.pop("duration_s")

    with pytest.raises(ValueError):
        parse_row(row)


def test_unhashable_request_id_is_rejected():
    row = _request_end_row()
    row["request_id"] = ["not", "hashable"]

    with pytest.raises(ValueError):
        parse_row(row)


def test_nullable_request_end_fields_may_be_null():
    record = RequestEnd(
        1,
        1.0,
        "trial",
        "request",
        200,
        "ok",
        None,
        None,
        None,
        None,
        None,
        8,
        None,
        None,
        1.0,
        0,
        None,
    )

    assert parse_row(record.to_row()) == record


def test_extra_fields_are_ignored():
    record = Close(1, 1.0, "trial")
    row = record.to_row()
    row["extra"] = "ignored"

    assert parse_row(row) == record


def test_sequence_gaps_are_reported():
    rows = [{"seq": seq} for seq in [1, 2, 4, 5, 9]]

    assert sequence_gaps(rows) == [(2, 4), (5, 9)]


def test_trial_rows_are_selected():
    rows = [{"trial": "a"}, {"trial": "b"}, {"trial": "a"}]

    assert rows_for_trial(rows, "a") == [rows[0], rows[2]]


def test_upstream_status_contract():
    assert UPSTREAM_STATUSES == (
        "ok",
        "rate_limited",
        "quota_exhausted",
        "server_error",
        "stalled",
        "truncated",
        "transport_error",
        "client_error",
        "client_disconnect",
    )
    assert (
        frozenset(
            {
                "rate_limited",
                "quota_exhausted",
                "server_error",
                "stalled",
                "truncated",
                "transport_error",
            }
        )
        == COUNTS_AGAINST_TRIAL
    )
