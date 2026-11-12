from __future__ import annotations

import json
import time

import httpx
import pytest

from harbor_research_runner.model_proxy.ledger import LedgerWriter
from harbor_research_runner.model_proxy.relay import RelayDeps, Route, relay_chat
from harbor_research_runner.records.jsonl import read_rows
from harbor_research_runner.records.limits import Limits
from harbor_research_runner.records.proxy_ledger import COUNTS_AGAINST_TRIAL
from tests.support.fake_upstream import FakeUpstream, RawResponse

_CARD = {"id": "card/model", "upstream_model": "served-model"}
_UNAVAILABLE = {"error": {"message": "upstream unavailable", "type": "upstream_unavailable"}}


def _deps(tmp_path, base_url, **limit_overrides):
    ledger = LedgerWriter(tmp_path / "proxy-ledger.jsonl")
    client = httpx.Client(trust_env=False)
    deps = RelayDeps(
        ledger=ledger,
        model=_CARD,
        limits=Limits.from_dict(limit_overrides, repetitions=1, verifier_timeout_multiplier=1.0),
        client=client,
        routes=lambda: iter([Route(f"{base_url}/v1/chat/completions", "test-key")]),
    )
    return deps, ledger, client


def _event(obj: dict) -> bytes:
    return f"data: {json.dumps(obj, separators=(',', ':'))}\n\n".encode()


def _request_body(stream: bool = True) -> bytes:
    return json.dumps(
        {
            "model": "card/model",
            "stream": stream,
            "messages": [{"role": "user", "content": "hello"}],
        }
    ).encode()


def _rows(tmp_path):
    return read_rows(tmp_path / "proxy-ledger.jsonl").rows


def _end_row(tmp_path):
    rows = [row for row in _rows(tmp_path) if row["type"] == "request_end"]
    assert len(rows) == 1
    return rows[0]


def test_client_disconnect_is_not_an_upstream_error(tmp_path):
    raw = RawResponse(
        [_event({"choices": [{"delta": {"content": "hi"}}]}), b"data: [DONE]\n\n"],
        hold_open=True,
    )
    upstream = FakeUpstream([raw], model="served-model")
    base_url = upstream.start()
    deps, ledger, client = _deps(tmp_path, base_url)
    try:
        response = relay_chat(_request_body(), {}, "trial-1", deps)
        response.close()
        assert raw.disconnected.wait(timeout=1.0)
    finally:
        raw.release.set()
        ledger.close()
        client.close()
        upstream.stop()

    end = _end_row(tmp_path)
    assert end["upstream_status"] == "client_disconnect"
    assert end["upstream_status"] not in COUNTS_AGAINST_TRIAL
    assert end["http_status"] == 200
    assert end["upstream_http_status"] == 200


def test_close_after_completion_does_not_add_another_row(tmp_path):
    upstream = FakeUpstream(["ok"], model="served-model")
    base_url = upstream.start()
    deps, ledger, client = _deps(tmp_path, base_url)
    try:
        response = relay_chat(_request_body(), {}, "trial-1", deps)
        list(response.chunks)
        response.close()
    finally:
        ledger.close()
        client.close()
        upstream.stop()

    end = _end_row(tmp_path)
    assert end["upstream_status"] == "ok"


@pytest.mark.parametrize(
    ("script", "expected_status", "expected_upstream_status"),
    [
        ("http_400", 400, "client_error"),
        ("http_502", 502, "server_error"),
        ("http_503", 503, "server_error"),
    ],
)
def test_upstream_errors_and_failovers_are_recorded(
    tmp_path, script, expected_status, expected_upstream_status
):
    upstream = FakeUpstream([script], model="served-model")
    base_url = upstream.start()
    deps, ledger, client = _deps(tmp_path, base_url)
    try:
        response = relay_chat(_request_body(), {}, "trial-1", deps)
        body = b"".join(response.chunks)
    finally:
        ledger.close()
        client.close()
        upstream.stop()

    end = _end_row(tmp_path)
    assert response.http_status == expected_status
    assert json.loads(body)["error"]["message"] == "scripted upstream error"
    assert end["http_status"] == expected_status
    assert end["upstream_status"] == expected_upstream_status
    assert end["upstream_http_status"] == expected_status
    assert "scripted upstream error" in end["error_excerpt"]
    assert end["failovers"] == 0


def test_first_chunk_stall_is_stalled_with_http_504(tmp_path):
    upstream = FakeUpstream(["stall:3"], model="served-model")
    base_url = upstream.start()
    deps, ledger, client = _deps(tmp_path, base_url, first_chunk_max_s=1.0)
    try:
        started = time.monotonic()
        response = relay_chat(_request_body(), {}, "trial-1", deps)
        elapsed = time.monotonic() - started
        body = b"".join(response.chunks)
    finally:
        ledger.close()
        client.close()
        upstream.stop()

    end = _end_row(tmp_path)
    assert response.http_status == 504
    assert elapsed < 2.0
    assert json.loads(body) == _UNAVAILABLE
    assert end["http_status"] == 504
    assert end["upstream_status"] == "stalled"
    assert end["upstream_http_status"] is None


def test_header_read_uses_the_smaller_total_deadline(tmp_path):
    upstream = FakeUpstream(["stall:3"], model="served-model")
    base_url = upstream.start()
    deps, ledger, client = _deps(
        tmp_path, base_url, first_chunk_max_s=2.0, request_latency_max_s=0.1
    )
    try:
        response = relay_chat(_request_body(), {}, "trial-1", deps)
        body = b"".join(response.chunks)
    finally:
        ledger.close()
        client.close()
        upstream.stop()

    end = _end_row(tmp_path)
    assert response.http_status == 504
    assert json.loads(body) == _UNAVAILABLE
    assert end["upstream_status"] == "stalled"
    assert end["duration_s"] < 0.5


def test_gap_stall_is_stalled_after_headers(tmp_path):
    upstream = FakeUpstream(["gap:3"], model="served-model")
    base_url = upstream.start()
    deps, ledger, client = _deps(tmp_path, base_url, inter_chunk_gap_max_s=1.0)
    try:
        response = relay_chat(_request_body(), {}, "trial-1", deps)
        started = time.monotonic()
        chunks = list(response.chunks)
        elapsed = time.monotonic() - started
    finally:
        ledger.close()
        client.close()
        upstream.stop()

    end = _end_row(tmp_path)
    assert response.http_status == 200
    assert elapsed < 2.0
    assert len(chunks) < 5
    assert b"data: [DONE]" not in b"".join(chunks)
    assert end["http_status"] == 200
    assert end["upstream_status"] == "stalled"
    assert end["upstream_http_status"] == 200


def test_gap_below_the_gap_limit_is_not_a_transport_error(tmp_path):
    upstream = FakeUpstream(["gap:1.5"], model="served-model")
    base_url = upstream.start()
    deps, ledger, client = _deps(
        tmp_path, base_url, first_chunk_max_s=1.0, inter_chunk_gap_max_s=5.0
    )
    try:
        response = relay_chat(_request_body(), {}, "trial-1", deps)
        chunks = list(response.chunks)
    finally:
        ledger.close()
        client.close()
        upstream.stop()

    end = _end_row(tmp_path)
    assert response.http_status == 200
    assert b"data: [DONE]" in b"".join(chunks)
    assert end["upstream_status"] == "ok"
    assert end["upstream_http_status"] == 200


def test_slow_consumer_receives_queued_chunks_without_a_gap_stall(tmp_path):
    chunks = [
        _event({"model": "served-model", "choices": [{"delta": {"content": "a"}}]}),
        _event({"model": "served-model", "choices": [{"delta": {"content": "b"}}]}),
        _event({"model": "served-model", "choices": [{"delta": {"content": "c"}}]}),
        b"data: [DONE]\n\n",
    ]
    upstream = FakeUpstream([RawResponse(chunks, chunk_delay_s=0.05)], model="served-model")
    base_url = upstream.start()
    deps, ledger, client = _deps(tmp_path, base_url, inter_chunk_gap_max_s=0.2)
    try:
        response = relay_chat(_request_body(), {}, "trial-1", deps)
        received = []
        iterator = iter(response.chunks)
        while True:
            try:
                chunk = next(iterator)
            except StopIteration:
                break
            received.append(chunk)
            time.sleep(0.5)
    finally:
        ledger.close()
        client.close()
        upstream.stop()

    end = _end_row(tmp_path)
    assert b"".join(received) == b"".join(chunks)
    assert end["upstream_status"] == "ok"


def test_request_latency_stall_is_stalled(tmp_path):
    upstream = FakeUpstream(["gap:3"], model="served-model")
    base_url = upstream.start()
    deps, ledger, client = _deps(tmp_path, base_url, request_latency_max_s=1.0)
    try:
        response = relay_chat(_request_body(), {}, "trial-1", deps)
        started = time.monotonic()
        chunks = list(response.chunks)
        elapsed = time.monotonic() - started
    finally:
        ledger.close()
        client.close()
        upstream.stop()

    end = _end_row(tmp_path)
    assert response.http_status == 200
    assert elapsed < 2.0
    assert len(chunks) < 5
    assert end["http_status"] == 200
    assert end["upstream_status"] == "stalled"
    assert end["upstream_http_status"] == 200


def test_truncated_stream_is_truncated(tmp_path):
    upstream = FakeUpstream(["truncate_after:2"], model="served-model")
    base_url = upstream.start()
    deps, ledger, client = _deps(tmp_path, base_url)
    try:
        response = relay_chat(_request_body(), {}, "trial-1", deps)
        list(response.chunks)
    finally:
        ledger.close()
        client.close()
        upstream.stop()

    end = _end_row(tmp_path)
    assert response.http_status == 200
    assert end["upstream_status"] == "truncated"
    assert end["upstream_http_status"] == 200


def test_drop_is_transport_error(tmp_path):
    upstream = FakeUpstream(["drop"], model="served-model")
    base_url = upstream.start()
    deps, ledger, client = _deps(tmp_path, base_url)
    try:
        response = relay_chat(_request_body(), {}, "trial-1", deps)
        body = b"".join(response.chunks)
    finally:
        ledger.close()
        client.close()
        upstream.stop()

    end = _end_row(tmp_path)
    assert response.http_status == 502
    assert json.loads(body) == _UNAVAILABLE
    assert end["http_status"] == 502
    assert end["upstream_status"] == "transport_error"
    assert end["upstream_http_status"] is None


def test_quota_is_quota_exhausted_and_keeps_retry_after(tmp_path):
    upstream = FakeUpstream(["quota"], model="served-model")
    base_url = upstream.start()
    deps, ledger, client = _deps(tmp_path, base_url)
    try:
        response = relay_chat(_request_body(), {}, "trial-1", deps)
        list(response.chunks)
    finally:
        ledger.close()
        client.close()
        upstream.stop()

    end = _end_row(tmp_path)
    assert response.http_status == 429
    assert response.headers["Retry-After"] == "86400"
    assert end["http_status"] == 429
    assert end["upstream_status"] == "quota_exhausted"
    assert end["upstream_http_status"] == 429
    assert "quota is exhausted" in end["error_excerpt"]


def test_huge_retry_after_records_end_and_closes_upstream_response(tmp_path):
    body = json.dumps(
        {"error": {"type": "FreeUsageLimitError", "message": "usage limit exceeded"}}
    ).encode()
    raw = RawResponse(
        [body],
        content_type="application/json",
        status_code=429,
        headers={"Retry-After": "9" * 400},
        wait_for_client_close=True,
    )
    upstream = FakeUpstream([raw], model="served-model")
    base_url = upstream.start()
    deps, ledger, client = _deps(tmp_path, base_url)
    try:
        response = relay_chat(_request_body(), {}, "trial-1", deps)
        response_body = b"".join(response.chunks)
        assert raw.client_closed.wait(timeout=1.0)
    finally:
        ledger.close()
        client.close()
        upstream.stop()

    ends = [row for row in _rows(tmp_path) if row["type"] == "request_end"]
    assert len(ends) == 1
    assert response.http_status == 429
    assert response_body == body
    assert ends[0]["upstream_status"] == "quota_exhausted"


def test_rate_limit_is_rate_limited_and_keeps_retry_after(tmp_path):
    upstream = FakeUpstream(["rate_limit"], model="served-model")
    base_url = upstream.start()
    deps, ledger, client = _deps(tmp_path, base_url)
    try:
        response = relay_chat(_request_body(), {}, "trial-1", deps)
        list(response.chunks)
    finally:
        ledger.close()
        client.close()
        upstream.stop()

    end = _end_row(tmp_path)
    assert response.http_status == 429
    assert response.headers["Retry-After"] == "1"
    assert end["http_status"] == 429
    assert end["upstream_status"] == "rate_limited"
    assert end["upstream_http_status"] == 429


def test_other_served_model_is_ok(tmp_path):
    upstream = FakeUpstream(["model:other"], model="served-model")
    base_url = upstream.start()
    deps, ledger, client = _deps(tmp_path, base_url)
    try:
        response = relay_chat(_request_body(), {}, "trial-1", deps)
        list(response.chunks)
    finally:
        ledger.close()
        client.close()
        upstream.stop()

    end = _end_row(tmp_path)
    assert response.http_status == 200
    assert end["upstream_status"] == "ok"
    assert end["upstream_http_status"] == 200
    assert end["served_model"] == "other"
