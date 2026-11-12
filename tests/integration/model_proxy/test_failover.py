from __future__ import annotations

import json

import httpx

from harbor_research_runner.model_proxy.adapters.chat import ChatAdapter
from harbor_research_runner.model_proxy.keys import KeyProvider
from harbor_research_runner.model_proxy.ledger import LedgerWriter
from harbor_research_runner.model_proxy.relay import RelayDeps, relay
from harbor_research_runner.records.jsonl import read_rows
from harbor_research_runner.records.limits import Limits
from tests.support.fake_upstream import FakeUpstream

_KEY = "sk-test-a"
_CARD = {"id": "card/model", "upstream_model": "served-model"}


def _request_body() -> bytes:
    return json.dumps(
        {
            "model": "card/model",
            "stream": True,
            "messages": [{"role": "user", "content": "hello"}],
        }
    ).encode()


def _run(tmp_path, first_script, second_script, caplog):
    first = FakeUpstream([first_script], model="first-model")
    second = FakeUpstream([second_script], model="second-model")
    first_url = first.start()
    second_url = second.start()
    ledger = LedgerWriter(tmp_path / "proxy-ledger.jsonl")
    client = httpx.Client(trust_env=False)
    card = {
        "endpoints": [
            f"{first_url}/v1/chat/completions",
            f"{second_url}/v1/chat/completions",
        ],
        "key_env": ["TEST_KEY"],
    }
    deps = RelayDeps(
        ledger=ledger,
        model=_CARD,
        limits=Limits.from_dict({}, repetitions=1, verifier_timeout_multiplier=1.0),
        client=client,
        routes=KeyProvider(card, {"TEST_KEY": _KEY}).routes,
    )
    try:
        response = relay(_request_body(), {}, "trial-1", deps, ChatAdapter())
        body = b"".join(response.chunks)
        rows = read_rows(tmp_path / "proxy-ledger.jsonl").rows
        ledger_text = (tmp_path / "proxy-ledger.jsonl").read_text(encoding="utf-8")
    finally:
        ledger.close()
        client.close()
        first.stop()
        second.stop()

    return response, body, rows, ledger_text, caplog.text, first, second


def test_failover_records_upstream_errors_and_recovery(tmp_path, caplog) -> None:
    response, _body, rows, ledger_text, log_text, first, second = _run(
        tmp_path, "http_502", "ok", caplog
    )

    ends = [row for row in rows if row["type"] == "request_end"]
    assert len(ends) == 1
    assert response.http_status == 200
    assert ends[0]["upstream_status"] == "ok"
    assert ends[0]["failovers"] == 1
    assert len(first.requests) == 1
    assert len(second.requests) == 1
    assert _KEY not in ledger_text
    assert _KEY not in log_text


def test_all_upstream_routes_fail_records_the_last_status(tmp_path, caplog) -> None:
    response, body, rows, _ledger_text, _log_text, first, second = _run(
        tmp_path, "http_502", "http_502", caplog
    )

    ends = [row for row in rows if row["type"] == "request_end"]
    assert response.http_status == 502
    assert json.loads(body)["error"]["message"] == "scripted upstream error"
    assert ends[0]["upstream_status"] == "server_error"
    assert ends[0]["upstream_http_status"] == 502
    assert ends[0]["failovers"] == 1
    assert len(first.requests) == 1
    assert len(second.requests) == 1


def test_rate_limited_response_fails_over(tmp_path, caplog) -> None:
    response, _body, rows, _ledger_text, _log_text, first, second = _run(
        tmp_path, "rate_limit", "ok", caplog
    )

    ends = [row for row in rows if row["type"] == "request_end"]
    assert response.http_status == 200
    assert ends[0]["upstream_status"] == "ok"
    assert ends[0]["failovers"] == 1
    assert len(first.requests) == 1
    assert len(second.requests) == 1


def test_transport_error_fails_over_before_a_response(tmp_path, caplog) -> None:
    response, _body, rows, _ledger_text, _log_text, first, second = _run(
        tmp_path, "drop", "ok", caplog
    )

    ends = [row for row in rows if row["type"] == "request_end"]
    assert response.http_status == 200
    assert ends[0]["upstream_status"] == "ok"
    assert ends[0]["failovers"] == 1
    assert len(first.requests) == 1
    assert len(second.requests) == 1


def test_client_error_does_not_fail_over(tmp_path, caplog) -> None:
    response, _body, rows, _ledger_text, _log_text, first, second = _run(
        tmp_path, "http_400", "ok", caplog
    )

    ends = [row for row in rows if row["type"] == "request_end"]
    assert response.http_status == 400
    assert ends[0]["upstream_status"] == "client_error"
    assert ends[0]["failovers"] == 0
    assert len(first.requests) == 1
    assert second.requests == []


def test_truncated_stream_does_not_fail_over(tmp_path, caplog) -> None:
    response, _body, rows, _ledger_text, _log_text, first, second = _run(
        tmp_path, "truncate_after:2", "ok", caplog
    )

    ends = [row for row in rows if row["type"] == "request_end"]
    assert response.http_status == 200
    assert ends[0]["upstream_status"] == "truncated"
    assert ends[0]["failovers"] == 0
    assert len(first.requests) == 1
    assert second.requests == []


def test_quota_response_does_not_fail_over(tmp_path, caplog) -> None:
    response, _body, rows, _ledger_text, _log_text, first, second = _run(
        tmp_path, "quota", "ok", caplog
    )

    ends = [row for row in rows if row["type"] == "request_end"]
    assert response.http_status == 429
    assert ends[0]["upstream_status"] == "quota_exhausted"
    assert ends[0]["failovers"] == 0
    assert len(first.requests) == 1
    assert second.requests == []
