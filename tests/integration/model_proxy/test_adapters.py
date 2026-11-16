from __future__ import annotations

import json
import time

import httpx
import pytest

from harbor_research_runner.model_proxy.adapters import select_adapter
from harbor_research_runner.model_proxy.ledger import LedgerWriter
from harbor_research_runner.model_proxy.relay import RelayDeps, Route, relay
from harbor_research_runner.records.jsonl import read_rows
from harbor_research_runner.records.limits import Limits
from tests.support.fake_upstream import FakeUpstream, RawResponse


def _request(*, stream: bool = True) -> bytes:
    return json.dumps(
        {
            "model": "card/model",
            "stream": stream,
            "messages": [{"role": "user", "content": "hello"}],
        }
    ).encode()


def _relay(tmp_path, upstream, upstream_api, request_path):
    ledger = LedgerWriter(tmp_path / "proxy-ledger.jsonl")
    client = httpx.Client(trust_env=False)
    card = {
        "id": "card/model",
        "upstream_model": "served-model",
        "api": upstream_api,
        "tokens_per_minute": None,
    }
    upstream_path = "/v1/responses" if upstream_api == "responses" else "/v1/chat/completions"
    deps = RelayDeps(
        ledger,
        card,
        Limits.from_dict({}, repetitions=1, verifier_timeout_multiplier=1.0),
        client,
        lambda: iter([Route(f"{upstream}{upstream_path}", "test-key")]),
        time.monotonic,
    )
    return ledger, client, deps, select_adapter(upstream_api, request_path)


def test_chat_request_is_translated_for_responses_upstream(tmp_path):
    upstream = FakeUpstream(["ok"], model="served-model")
    upstream_url = upstream.start()
    ledger, client, deps, adapter = _relay(
        tmp_path, upstream_url, "responses", "/v1/chat/completions"
    )
    try:
        response = relay(_request(), {}, "trial-1", deps, adapter)
        chunks = list(response.chunks)
    finally:
        ledger.close()
        client.close()
        upstream.stop()

    sent = upstream.requests[0]["body"]
    assert sent["model"] == "served-model"
    assert sent["input"] == [{"role": "user", "content": "hello"}]
    assert "messages" not in sent
    assert b"data: [DONE]" in b"".join(chunks)
    end = read_rows(tmp_path / "proxy-ledger.jsonl").rows[-1]
    assert end["type"] == "request_end"
    assert (end["prompt_tokens"], end["completion_tokens"]) == (100, 20)
    assert end["served_model"] == "served-model"


def test_chat_identity_stream_preserves_upstream_bytes(tmp_path):
    raw = [
        b": keepalive\r\n\r\n",
        b'data: {"model":"served-model","choices":[{"delta":{"content":"hello"}}]}\r\n\r\n',
        b"data: [DONE]\r\n\r\n",
    ]
    upstream = FakeUpstream([RawResponse(raw)], model="served-model")
    upstream_url = upstream.start()
    ledger, client, deps, adapter = _relay(tmp_path, upstream_url, "chat", "/v1/chat/completions")
    try:
        response = relay(_request(), {}, "trial-1", deps, adapter)
        chunks = list(response.chunks)
    finally:
        ledger.close()
        client.close()
        upstream.stop()

    assert b"".join(chunks) == b"".join(raw)


def test_responses_identity_stream_preserves_upstream_bytes(tmp_path):
    raw = [
        b'event: response.created\r\ndata: {"type":"response.created","response":{"id":"resp_1","model":"served-model"}}\r\n\r\n',
        b'event: response.completed\r\ndata: {"type":"response.completed","response":{"id":"resp_1","model":"served-model","status":"completed","output":[],"usage":{"input_tokens":1,"output_tokens":1}}}\r\n\r\n',
    ]
    upstream = FakeUpstream([RawResponse(raw)], model="served-model")
    upstream_url = upstream.start()
    ledger, client, deps, adapter = _relay(tmp_path, upstream_url, "responses", "/v1/responses")
    try:
        response = relay(_request(), {}, "trial-1", deps, adapter)
        chunks = list(response.chunks)
    finally:
        ledger.close()
        client.close()
        upstream.stop()

    assert b"".join(chunks) == b"".join(raw)


def test_chat_stream_to_responses_records_cached_read_tokens(tmp_path):
    raw = [
        b'event: response.completed\r\ndata: {"type":"response.completed","response":{"id":"resp_1","model":"served-model","status":"completed","output":[],"usage":{"input_tokens":12,"output_tokens":3,"input_tokens_details":{"cached_tokens":7}}}}\r\n\r\n',
    ]
    upstream = FakeUpstream([RawResponse(raw)], model="served-model")
    upstream_url = upstream.start()
    ledger, client, deps, adapter = _relay(
        tmp_path, upstream_url, "responses", "/v1/chat/completions"
    )
    try:
        response = relay(_request(), {}, "trial-1", deps, adapter)
        list(response.chunks)
    finally:
        ledger.close()
        client.close()
        upstream.stop()

    end = read_rows(tmp_path / "proxy-ledger.jsonl").rows[-1]
    assert end["cached_read_tokens"] == 7


@pytest.mark.parametrize(
    ("event_type", "status"),
    [("response.failed", "failed"), ("response.completed", "failed")],
    ids=["failed-event", "non-ok-status"],
)
def test_responses_identity_failed_event_counts_as_non_ok(tmp_path, event_type, status):
    raw = [
        (
            f"event: {event_type}\r\ndata: "
            f'{{"type":"{event_type}","response":{{"status":"{status}",'
            '"model":"served-model"}}\r\n\r\n'
        ).encode(),
        b'event: response.completed\r\ndata: {"type":"response.completed","response":{"status":"completed","model":"served-model"}}\r\n\r\n',
    ]
    upstream = FakeUpstream([RawResponse(raw)], model="served-model")
    upstream_url = upstream.start()
    ledger, client, deps, adapter = _relay(tmp_path, upstream_url, "responses", "/v1/responses")
    try:
        response = relay(_request(), {}, "trial-1", deps, adapter)
        response_chunks = list(response.chunks)
    finally:
        ledger.close()
        client.close()
        upstream.stop()

    end = read_rows(tmp_path / "proxy-ledger.jsonl").rows[-1]
    assert b"".join(response_chunks) == b"".join(raw)
    assert end["upstream_status"] != "ok"


def test_responses_identity_multiline_sse_records_completion_usage_and_model(tmp_path):
    raw = [
        b'event: response.output_text.delta\r\ndata: {"type":"response.output_text.delta",\r\ndata: "delta":"hello",\r\ndata: "model":"served-model"}\r\n\r\n',
        b'event: response.completed\r\ndata: {"type":"response.completed",\r\ndata: "response":{"status":"completed",\r\ndata: "model":"served-model","output":[],\r\ndata: "usage":{"input_tokens":4,"output_tokens":2,"input_tokens_details":{"cached_tokens":1}}}}\r\n\r\n',
    ]
    upstream = FakeUpstream([RawResponse(raw)], model="served-model")
    upstream_url = upstream.start()
    ledger, client, deps, adapter = _relay(tmp_path, upstream_url, "responses", "/v1/responses")
    try:
        response = relay(_request(), {}, "trial-1", deps, adapter)
        chunks = list(response.chunks)
    finally:
        ledger.close()
        client.close()
        upstream.stop()

    end = read_rows(tmp_path / "proxy-ledger.jsonl").rows[-1]
    assert b"".join(chunks) == b"".join(raw)
    assert end["completion_chars"] == 5
    assert (end["prompt_tokens"], end["completion_tokens"], end["cached_read_tokens"]) == (
        4,
        2,
        1,
    )
    assert end["served_model"] == "served-model"


@pytest.mark.parametrize(
    "terminator",
    [b"data: [DONE]", b"data: [DONE]\n"],
    ids=["without-newline", "with-newline"],
)
def test_chat_identity_final_done_remainder_records_ok_and_usage(tmp_path, terminator):
    raw = [
        b'data: {"model":"served-model","choices":[],"usage":{"prompt_tokens":4,"completion_tokens":2,"total_tokens":6}}\n\n',
        terminator,
    ]
    upstream = FakeUpstream([RawResponse(raw)], model="served-model")
    upstream_url = upstream.start()
    ledger, client, deps, adapter = _relay(tmp_path, upstream_url, "chat", "/v1/chat/completions")
    try:
        response = relay(_request(), {}, "trial-1", deps, adapter)
        chunks = list(response.chunks)
    finally:
        ledger.close()
        client.close()
        upstream.stop()

    end = read_rows(tmp_path / "proxy-ledger.jsonl").rows[-1]
    assert b"".join(chunks) == b"".join(raw)
    assert end["upstream_status"] == "ok"
    assert (end["prompt_tokens"], end["completion_tokens"]) == (4, 2)


def test_responses_identity_multiline_final_remainder_records_usage_and_model(tmp_path):
    raw = [
        b'event: response.completed\r\ndata: {"type":"response.completed",\r\ndata: "response":{"status":"completed",\r\ndata: "model":"served-model","output":[],\r\ndata: "usage":{"input_tokens":6,"output_tokens":2}}}'
    ]
    upstream = FakeUpstream([RawResponse(raw)], model="served-model")
    upstream_url = upstream.start()
    ledger, client, deps, adapter = _relay(tmp_path, upstream_url, "responses", "/v1/responses")
    try:
        response = relay(_request(), {}, "trial-1", deps, adapter)
        chunks = list(response.chunks)
    finally:
        ledger.close()
        client.close()
        upstream.stop()

    end = read_rows(tmp_path / "proxy-ledger.jsonl").rows[-1]
    assert b"".join(chunks) == b"".join(raw)
    assert end["upstream_status"] == "ok"
    assert (end["prompt_tokens"], end["completion_tokens"]) == (6, 2)
    assert end["served_model"] == "served-model"


@pytest.mark.parametrize(
    "body",
    [{}, {"status": "completed"}],
    ids=["missing-status-and-output", "missing-output"],
)
def test_responses_nonstream_missing_required_fields_counts_as_truncated(tmp_path, body):
    upstream = FakeUpstream(
        [RawResponse([json.dumps(body).encode()], content_type="application/json")],
        model="served-model",
    )
    upstream_url = upstream.start()
    ledger, client, deps, adapter = _relay(
        tmp_path, upstream_url, "responses", "/v1/chat/completions"
    )
    try:
        response = relay(_request(stream=False), {}, "trial-1", deps, adapter)
        list(response.chunks)
    finally:
        ledger.close()
        client.close()
        upstream.stop()

    end = read_rows(tmp_path / "proxy-ledger.jsonl").rows[-1]
    assert end["upstream_status"] == "truncated"


@pytest.mark.parametrize(
    "response_body",
    [{"output": []}, {"status": "completed"}],
    ids=["missing-status", "missing-output"],
)
def test_responses_completed_event_missing_required_fields_counts_as_truncated(
    tmp_path, response_body
):
    event = {"type": "response.completed", "response": response_body}
    raw = [b"event: response.completed\r\ndata: " + json.dumps(event).encode() + b"\r\n\r\n"]
    upstream = FakeUpstream(
        [RawResponse(raw, content_type="text/event-stream")], model="served-model"
    )
    upstream_url = upstream.start()
    ledger, client, deps, adapter = _relay(
        tmp_path, upstream_url, "responses", "/v1/chat/completions"
    )
    try:
        response = relay(_request(), {}, "trial-1", deps, adapter)
        list(response.chunks)
    finally:
        ledger.close()
        client.close()
        upstream.stop()

    end = read_rows(tmp_path / "proxy-ledger.jsonl").rows[-1]
    assert end["upstream_status"] == "truncated"
