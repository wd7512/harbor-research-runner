from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import pytest

from harbor_research_runner.model_proxy.status import (
    UpstreamOutcome,
    classify,
    retry_after_seconds,
    status_for_harness,
)

NOW = datetime(2026, 11, 11, 12, 0, 0, tzinfo=UTC)


def _outcome(**overrides) -> UpstreamOutcome:
    values = {
        "http_status": 200,
        "retry_after_s": None,
        "error_body": "",
        "got_terminator": True,
        "cut_by_proxy": None,
        "transport_failed": False,
        "client_disconnected": False,
    }
    values.update(overrides)
    return UpstreamOutcome(**values)


def _classify_429(body: str | None, retry_after: str | None) -> str:
    return classify(
        _outcome(
            http_status=429,
            retry_after_s=retry_after_seconds(retry_after, now=NOW),
            error_body=body or "",
        )
    )


def test_quota_on_free_usage_limit_error() -> None:
    body = json.dumps({"error": {"type": "FreeUsageLimitError", "code": 429}})

    assert _classify_429(body, None) == "quota_exhausted"


def test_quota_on_usage_limit_phrasing() -> None:
    assert (
        _classify_429('{"error": {"message": "monthly usage limit exceeded"}}', None)
        == "quota_exhausted"
    )


def test_short_retry_after_overrules_quota_marker() -> None:
    body = json.dumps({"error": {"type": "FreeUsageLimitError"}})

    assert _classify_429(body, "12") == "rate_limited"


def test_quota_on_go_usage_limit_with_long_retry_after() -> None:
    body = json.dumps(
        {
            "error": {
                "type": "GoUsageLimitError",
                "message": "Go usage limit exceeded",
                "metadata": {"limitName": "weekly"},
            }
        }
    )

    assert _classify_429(body, "223920") == "quota_exhausted"


def test_quota_on_long_http_date_retry_after() -> None:
    body = json.dumps({"error": {"type": "SomethingWeHaveNeverSeen"}})
    future = format_datetime(NOW + timedelta(days=2), usegmt=True)

    assert _classify_429(body, future) == "quota_exhausted"


def test_rate_limit_on_short_http_date_retry_after() -> None:
    body = json.dumps({"error": {"type": "SomethingWeHaveNeverSeen"}})
    soon = format_datetime(NOW + timedelta(seconds=45), usegmt=True)

    assert _classify_429(body, soon) == "rate_limited"


def test_unparseable_retry_after_falls_back_to_markers() -> None:
    assert _classify_429(json.dumps({"error": {"type": "FreeUsageLimitError"}}), "later") == (
        "quota_exhausted"
    )
    assert _classify_429(json.dumps({"error": {"type": "RateLimitError"}}), "later") == (
        "rate_limited"
    )


def test_unknown_429_warns(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="harbor_research_runner.model_proxy.status"):
        assert _classify_429(json.dumps({"error": {"type": "BrandNewError"}}), None) == (
            "rate_limited"
        )

    assert "BrandNewError" in caplog.text


def test_recognized_burst_does_not_warn(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="harbor_research_runner.model_proxy.status"):
        assert _classify_429(json.dumps({"error": {"type": "RateLimitError"}}), None) == (
            "rate_limited"
        )

    assert caplog.text == ""


def test_retry_after_overrules_prose_quota_mention() -> None:
    body = json.dumps({"error": {"message": "per-minute token quota exceeded"}})

    assert _classify_429(body, "30") == "rate_limited"


def test_quota_phrasing_without_usage_limit_stays_burst() -> None:
    assert (
        _classify_429('{"error": {"message": "per-minute token quota exceeded"}}', None)
        == "rate_limited"
    )


def test_quota_on_usage_limit_phrasing_without_header() -> None:
    assert _classify_429('{"error": {"message": "monthly usage-limit exceeded"}}', None) == (
        "quota_exhausted"
    )


def test_rate_limit_on_retry_after() -> None:
    assert _classify_429('{"error": {"message": "slow down"}}', "30") == "rate_limited"


def test_rate_limit_on_rate_limit_body_without_header() -> None:
    assert _classify_429('{"error": {"type": "RateLimitError"}}', None) == "rate_limited"


def test_ambiguous_defaults_to_rate_limit() -> None:
    assert _classify_429(None, None) == "rate_limited"
    assert _classify_429("", None) == "rate_limited"
    assert _classify_429("garbage bytes \xff\xfe", None) == "rate_limited"


def test_classify_case_insensitive() -> None:
    assert _classify_429('{"error": {"type": "FREEUSAGELIMITERROR"}}', None) == ("quota_exhausted")


def test_client_disconnect_wins_over_a_server_error() -> None:
    outcome = _outcome(http_status=502, client_disconnected=True)

    assert classify(outcome) == "client_disconnect"


@pytest.mark.parametrize("reason", ["first_chunk", "gap", "latency"])
def test_proxy_cut_is_stalled(reason: str) -> None:
    assert classify(_outcome(http_status=200, cut_by_proxy=reason)) == "stalled"


def test_transport_failure_is_transport_error() -> None:
    outcome = _outcome(http_status=None, transport_failed=True)

    assert classify(outcome) == "transport_error"


def test_server_error_is_5xx() -> None:
    assert classify(_outcome(http_status=503, got_terminator=False)) == "server_error"


def test_client_error_is_other_4xx() -> None:
    assert classify(_outcome(http_status=400, got_terminator=False)) == "client_error"


def test_200_without_terminator_is_truncated() -> None:
    assert classify(_outcome(http_status=200, got_terminator=False)) == "truncated"


def test_200_with_terminator_is_ok() -> None:
    assert classify(_outcome(http_status=200, got_terminator=True)) == "ok"


def test_unlisted_http_status_is_server_error() -> None:
    assert classify(_outcome(http_status=302, got_terminator=False)) == "server_error"


def test_no_response_without_transport_failure_is_server_error() -> None:
    assert classify(_outcome(http_status=None, got_terminator=False)) == "server_error"


def test_status_for_harness_maps_stalled_to_504() -> None:
    assert status_for_harness("stalled", 200) == 504


def test_status_for_harness_maps_transport_error_to_502() -> None:
    assert status_for_harness("transport_error", None) == 502


def test_status_for_harness_keeps_the_upstream_status() -> None:
    assert status_for_harness("server_error", 503) == 503


def test_status_for_harness_without_an_upstream_status_is_502() -> None:
    assert status_for_harness("server_error", None) == 502


def test_retry_after_seconds_reads_delta_seconds() -> None:
    assert retry_after_seconds("12", now=NOW) == 12.0


def test_retry_after_seconds_reads_an_http_date() -> None:
    value = format_datetime(NOW + timedelta(seconds=45), usegmt=True)

    assert retry_after_seconds(value, now=NOW) == pytest.approx(45.0, abs=1.0)


def test_retry_after_seconds_floors_past_http_dates_at_zero() -> None:
    value = format_datetime(NOW - timedelta(seconds=45), usegmt=True)

    assert retry_after_seconds(value, now=NOW) == 0.0


def test_negative_retry_after_falls_back_to_quota_body() -> None:
    body = json.dumps({"error": {"type": "FreeUsageLimitError"}})

    assert _classify_429(body, "-1") == "quota_exhausted"


def test_retry_after_seconds_rejects_oversized_delta_seconds() -> None:
    assert retry_after_seconds("9" * 400, now=NOW) is None


def test_retry_after_seconds_rejects_signed_positive_delta_seconds() -> None:
    assert retry_after_seconds("+5", now=NOW) is None


def test_retry_after_seconds_accepts_zero_delta_seconds() -> None:
    assert retry_after_seconds("0", now=NOW) == 0.0


@pytest.mark.parametrize("value", [None, "", "later", "1.5"])
def test_retry_after_seconds_returns_none_when_unusable(value: str | None) -> None:
    assert retry_after_seconds(value, now=NOW) is None
