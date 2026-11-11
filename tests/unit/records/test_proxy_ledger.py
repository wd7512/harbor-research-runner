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


def test_unknown_upstream_status_is_rejected():
    row = RequestEnd(
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
    row["upstream_status"] = "unknown"

    with pytest.raises(ValueError):
        parse_row(row)


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
