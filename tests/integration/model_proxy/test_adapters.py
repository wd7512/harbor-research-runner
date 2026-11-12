from __future__ import annotations

import json
import time

import httpx

from harbor_research_runner.model_proxy.adapters import select_adapter
from harbor_research_runner.model_proxy.ledger import LedgerWriter
from harbor_research_runner.model_proxy.relay import RelayDeps, Route, relay
from harbor_research_runner.records.jsonl import read_rows
from harbor_research_runner.records.limits import Limits
from tests.support.fake_upstream import FakeUpstream, RawResponse


def _request() -> bytes:
    return json.dumps(
        {
            "model": "card/model",
            "stream": True,
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
        b'event: response.completed\r\ndata: {"type":"response.completed","response":{"id":"resp_1","model":"served-model","status":"completed","usage":{"input_tokens":1,"output_tokens":1}}}\r\n\r\n',
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
