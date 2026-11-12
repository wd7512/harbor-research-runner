from __future__ import annotations

import json
import time
from threading import Event, Thread

import httpx
import pytest

from harbor_research_runner.model_proxy.adapters.chat import ChatAdapter
from harbor_research_runner.model_proxy.bodies import BodyStore
from harbor_research_runner.model_proxy.ledger import LedgerWriter
from harbor_research_runner.model_proxy.pacing import Pacer
from harbor_research_runner.model_proxy.relay import RelayDeps, Route, relay
from harbor_research_runner.records.jsonl import read_rows
from harbor_research_runner.records.limits import Limits
from tests.support.fake_upstream import FakeUpstream, RawResponse

_CARD = {"id": "card/model", "upstream_model": "served-model"}


def _deps(tmp_path, base_url, now=time.monotonic, *, pacer=None, bodies=None, routes=None):
    ledger = LedgerWriter(tmp_path / "proxy-ledger.jsonl")
    client = httpx.Client(trust_env=False)
    deps = RelayDeps(
        ledger=ledger,
        model=_CARD,
        limits=Limits.from_dict({}, repetitions=1, verifier_timeout_multiplier=1.0),
        client=client,
        routes=routes or (lambda: iter([Route(f"{base_url}/v1/chat/completions", "test-key")])),
        now=now,
        pacer=pacer,
        bodies=bodies,
    )
    return deps, ledger, client


def _event(obj: dict) -> bytes:
    return f"data: {json.dumps(obj, separators=(',', ':'))}\n\n".encode()


class _Clock:
    def __init__(self, values):
        self._values = iter(values)
        self.calls = 0
        self.first_mark = Event()

    def __call__(self):
        value = next(self._values)
        if self.calls == 1:
            self.first_mark.set()
        self.calls += 1
        return value


def _request_body(stream: bool) -> bytes:
    return json.dumps(
        {
            "model": "card/model",
            "stream": stream,
            "messages": [{"role": "user", "content": "hello"}],
        }
    ).encode()


def _rows(tmp_path):
    return read_rows(tmp_path / "proxy-ledger.jsonl").rows


def test_pacing_wait_is_recorded_before_each_request_start(tmp_path):
    class Clock:
        value = 0.0

        def now(self):
            return self.value

        def sleep(self, duration):
            self.value += duration

    clock = Clock()
    pacer = Pacer(120, clock.now, clock.sleep, cooldown_until=2.5)
    upstream = FakeUpstream(["ok", "ok"], model="served-model")
    base_url = upstream.start()
    deps, ledger, client = _deps(tmp_path, base_url, now=clock.now, pacer=pacer)
    try:
        first = relay(_request_body(True), {}, "trial-1", deps, ChatAdapter())
        list(first.chunks)
        second = relay(_request_body(True), {}, "trial-2", deps, ChatAdapter())
        list(second.chunks)
    finally:
        ledger.close()
        client.close()
        upstream.stop()

    starts = [row for row in _rows(tmp_path) if row["type"] == "request_start"]
    assert [row["paced_wait_s"] for row in starts] == [2.5, 60.0]


def test_body_store_records_the_response_bytes_sent_to_the_harness(tmp_path):
    upstream = FakeUpstream(["ok"], model="served-model")
    base_url = upstream.start()
    bodies = BodyStore(tmp_path / "bodies")
    (tmp_path / "bodies").mkdir()
    deps, ledger, client = _deps(tmp_path, base_url, bodies=bodies)
    request_body = _request_body(True)
    try:
        response = relay(request_body, {}, "trial-1", deps, ChatAdapter())
        response_bytes = b"".join(response.chunks)
    finally:
        ledger.close()
        client.close()
        upstream.stop()

    body_rows = read_rows(tmp_path / "bodies" / "trial-1.jsonl").rows
    assert len(body_rows) == 1
    assert body_rows[0]["request"] == request_body.decode()
    assert body_rows[0]["response"].encode() == response_bytes
    assert _rows(tmp_path)[-1]["type"] == "request_end"


def test_request_without_an_upstream_route_is_recorded_as_transport_error(tmp_path):
    ledger = LedgerWriter(tmp_path / "proxy-ledger.jsonl")
    client = httpx.Client(trust_env=False)
    deps = RelayDeps(
        ledger,
        _CARD,
        Limits.from_dict({}, repetitions=1, verifier_timeout_multiplier=1.0),
        client,
        lambda: iter(()),
    )
    try:
        response = relay(_request_body(True), {}, "trial-1", deps, ChatAdapter())
        body = b"".join(response.chunks)
    finally:
        ledger.close()
        client.close()

    rows = _rows(tmp_path)
    assert [row["type"] for row in rows] == ["request_start", "request_end"]
    assert rows[-1]["upstream_status"] == "transport_error"
    assert rows[-1]["http_status"] == 502
    assert b"upstream unavailable" in body


def test_ok_stream_returns_upstream_status_and_content_type(tmp_path):
    upstream = FakeUpstream(["ok"], model="served-model")
    base_url = upstream.start()
    deps, ledger, client = _deps(tmp_path, base_url)
    try:
        response = relay(_request_body(True), {}, "trial-1", deps, ChatAdapter())
        chunks = list(response.chunks)
    finally:
        ledger.close()
        client.close()
        upstream.stop()

    assert response.http_status == 200
    assert response.headers["Content-Type"] == "text/event-stream"
    assert b"data: [DONE]" in b"".join(chunks)


def test_ok_stream_writes_one_start_and_one_end_row_with_usage(tmp_path):
    upstream = FakeUpstream(["ok"], model="served-model")
    base_url = upstream.start()
    deps, ledger, client = _deps(tmp_path, base_url)
    try:
        response = relay(_request_body(True), {}, "trial-1", deps, ChatAdapter())
        list(response.chunks)
    finally:
        ledger.close()
        client.close()
        upstream.stop()

    rows = _rows(tmp_path)
    assert [row["type"] for row in rows] == ["request_start", "request_end"]
    start, end = rows
    assert start["trial"] == "trial-1"
    assert start["model"] == "card/model"
    assert start["is_stream"] is True
    assert start["prompt_chars"] == len("hello")
    assert start["paced_wait_s"] == 0.0
    assert end["trial"] == "trial-1"
    assert end["request_id"] == start["request_id"]
    assert start["seq"] < end["seq"]
    assert end["http_status"] == 200
    assert end["upstream_status"] == "ok"
    assert end["upstream_http_status"] == 200
    assert end["served_model"] == "served-model"
    assert (end["prompt_tokens"], end["completion_tokens"], end["cached_read_tokens"]) == (
        100,
        20,
        0,
    )
    assert end["completion_chars"] == len("hello")
    assert end["failovers"] == 0


def test_ok_stream_substitutes_the_card_upstream_model(tmp_path):
    upstream = FakeUpstream(["ok"], model="served-model")
    base_url = upstream.start()
    deps, ledger, client = _deps(tmp_path, base_url)
    try:
        response = relay(_request_body(True), {}, "trial-1", deps, ChatAdapter())
        list(response.chunks)
    finally:
        ledger.close()
        client.close()
        upstream.stop()

    assert upstream.requests[0]["body"]["model"] == "served-model"
    assert upstream.requests[0]["headers"]["authorization"] == "Bearer test-key"


def test_end_row_is_written_before_the_final_chunk_is_yielded(tmp_path):
    upstream = FakeUpstream(["ok"], model="served-model")
    base_url = upstream.start()
    deps, ledger, client = _deps(tmp_path, base_url)
    try:
        response = relay(_request_body(True), {}, "trial-1", deps, ChatAdapter())
        chunks = []
        ends_after_chunk = []
        for chunk in response.chunks:
            chunks.append(chunk)
            ends_after_chunk.append(
                [row for row in _rows(tmp_path) if row["type"] == "request_end"]
            )
    finally:
        ledger.close()
        client.close()
        upstream.stop()

    assert len(chunks) == 7
    assert ends_after_chunk[-2]
    assert not ends_after_chunk[0]


def test_non_streaming_request_records_start_and_end_rows(tmp_path):
    upstream = FakeUpstream(["ok"], model="served-model")
    base_url = upstream.start()
    deps, ledger, client = _deps(tmp_path, base_url)
    try:
        response = relay(_request_body(False), {}, "trial-1", deps, ChatAdapter())
        assert not [row for row in _rows(tmp_path) if row["type"] == "request_end"]

        chunks = list(response.chunks)
    finally:
        ledger.close()
        client.close()
        upstream.stop()

    rows = _rows(tmp_path)
    assert [row["type"] for row in rows] == ["request_start", "request_end"]
    start, end = rows
    assert response.http_status == 200
    assert response.headers["Content-Type"] == "application/json"
    assert len(chunks) == 1
    assert json.loads(chunks[0])["choices"][0]["message"]["content"] == "hello"
    assert start["is_stream"] is False
    assert end["request_id"] == start["request_id"]
    assert start["seq"] < end["seq"]
    assert end["upstream_status"] == "ok"
    assert (end["prompt_tokens"], end["completion_tokens"], end["cached_read_tokens"]) == (
        100,
        20,
        0,
    )
    assert end["completion_chars"] == len("hello")
    assert upstream.requests[0]["body"]["model"] == "served-model"


def test_truncated_stream_is_recorded_as_truncated(tmp_path):
    upstream = FakeUpstream(["truncate_after:3"], model="served-model")
    base_url = upstream.start()
    deps, ledger, client = _deps(tmp_path, base_url)
    try:
        response = relay(_request_body(True), {}, "trial-1", deps, ChatAdapter())
        chunks = list(response.chunks)
    finally:
        ledger.close()
        client.close()
        upstream.stop()

    end = _rows(tmp_path)[-1]
    assert len(chunks) == 3
    assert end["type"] == "request_end"
    assert end["upstream_status"] == "truncated"
    assert end["upstream_http_status"] == 200
    assert end["prompt_tokens"] is None
    assert end["completion_chars"] == len("hel")


@pytest.mark.parametrize("script", ["http_500"])
def test_upstream_error_is_recorded_as_server_error(tmp_path, script):
    upstream = FakeUpstream([script], model="served-model")
    base_url = upstream.start()
    deps, ledger, client = _deps(tmp_path, base_url)
    try:
        response = relay(_request_body(True), {}, "trial-1", deps, ChatAdapter())
        chunks = list(response.chunks)
    finally:
        ledger.close()
        client.close()
        upstream.stop()

    end = _rows(tmp_path)[-1]
    assert response.http_status == 500
    assert len(chunks) == 1
    assert json.loads(chunks[0])["error"]
    assert end["type"] == "request_end"
    assert end["http_status"] == 500
    assert end["upstream_status"] == "server_error"
    assert end["upstream_http_status"] == 500


def test_stream_observes_usage_on_early_content_through_the_terminator(tmp_path):
    events = [
        _event(
            {
                "model": "early",
                "choices": [{"delta": {"content": "hel"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 1},
            }
        ),
        _event(
            {
                "model": "middle",
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {"function": {"name": "lookup", "arguments": '{"q":"x"}'}}
                            ]
                        }
                    }
                ],
                "usage": {"prompt_tokens": 50, "completion_tokens": 3},
            }
        ),
        _event({"model": "middle", "choices": [{"delta": {"content": "lo"}}]}),
        _event(
            {
                "model": "final-model",
                "choices": [],
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 20,
                    "prompt_tokens_details": {"cached_tokens": 3},
                },
            }
        ),
        b"data: [DONE]\n\n",
    ]
    upstream = FakeUpstream([RawResponse(events)], model="served-model")
    base_url = upstream.start()
    deps, ledger, client = _deps(tmp_path, base_url)
    try:
        response = relay(_request_body(True), {}, "trial-1", deps, ChatAdapter())
        iterator = response.chunks
        first = next(iterator)
        rows_after_first = _rows(tmp_path)
        rest = list(iterator)
    finally:
        ledger.close()
        client.close()
        upstream.stop()

    assert not [row for row in rows_after_first if row["type"] == "request_end"]
    assert b"".join([first, *rest]) == b"".join(events)
    rows = _rows(tmp_path)
    assert [row["type"] for row in rows] == ["request_start", "request_end"]
    end = rows[-1]
    assert (end["prompt_tokens"], end["completion_tokens"], end["cached_read_tokens"]) == (
        100,
        20,
        3,
    )
    assert end["completion_chars"] == 3 + len("lookup") + len('{"q":"x"}') + 2
    assert end["served_model"] == "final-model"


def test_nonterminal_event_is_forwarded_before_the_next_event_arrives(tmp_path):
    gate = Event()
    events = [
        _event({"model": "served-model", "choices": [{"delta": {"content": "hi"}}]}),
        b"data: [DONE]\n\n",
    ]
    raw = RawResponse(events, gate=gate, gate_after=1)
    upstream = FakeUpstream([raw], model="served-model")
    base_url = upstream.start()
    deps, ledger, client = _deps(tmp_path, base_url)
    response = relay(_request_body(True), {}, "trial-1", deps, ChatAdapter())
    first = []
    received = Event()

    def consume_first():
        first.append(next(response.chunks))
        received.set()

    thread = Thread(target=consume_first, daemon=True)
    thread.start()
    rest = []
    try:
        assert received.wait(timeout=0.5)
        assert not [row for row in _rows(tmp_path) if row["type"] == "request_end"]
    finally:
        gate.set()
        thread.join(timeout=2)
        if not thread.is_alive():
            rest.extend(response.chunks)
        response.close()
        ledger.close()
        client.close()
        upstream.stop()

    assert first == [events[0]]
    assert first + rest == events
    end = _rows(tmp_path)[-1]
    assert end["upstream_status"] == "ok"


def test_nonstream_first_chunk_is_stamped_before_the_body_completes(tmp_path):
    gate = Event()
    body = (
        b'{"model":"served-model","choices":[{"message":{"content":"hel'
        b'lo"}}],"usage":{"prompt_tokens":1,"completion_tokens":2}}'
    )
    cut = body.index(b'"hello"') + len(b'"hel')
    raw = RawResponse(
        [body[:cut], body[cut:]],
        gate=gate,
        gate_after=1,
        content_type="application/json",
    )
    upstream = FakeUpstream([raw], model="served-model")
    base_url = upstream.start()
    clock = _Clock([0.0, 5.0, 9.0, 12.0])
    deps, ledger, client = _deps(tmp_path, base_url, now=clock)
    response = relay(_request_body(False), {}, "trial-1", deps, ChatAdapter())
    chunks = []
    received = Event()

    def consume():
        chunks.append(next(response.chunks))
        received.set()

    thread = Thread(target=consume, daemon=True)
    thread.start()
    rest = []
    try:
        assert clock.first_mark.wait(timeout=1.0)
        assert not received.is_set()
        assert not [row for row in _rows(tmp_path) if row["type"] == "request_end"]
    finally:
        gate.set()
        thread.join(timeout=2)
        if not thread.is_alive():
            rest.extend(response.chunks)
        response.close()
        ledger.close()
        client.close()
        upstream.stop()

    assert chunks + rest == [body]
    end = _rows(tmp_path)[-1]
    assert end["first_chunk_s"] == 5.0
    assert end["first_content_s"] == 9.0
    assert end["duration_s"] == 12.0
    assert end["upstream_status"] == "ok"


def test_stream_starting_with_empty_usage_keeps_streaming(tmp_path):
    events = [
        _event({"model": "early", "choices": [{"delta": {"role": "assistant"}}], "usage": {}}),
        _event({"model": "early", "choices": [{"delta": {"content": "hi"}}]}),
        _event(
            {
                "model": "final-model",
                "choices": [],
                "usage": {"prompt_tokens": 1, "completion_tokens": 2},
            }
        ),
        b"data: [DONE]\n\n",
    ]
    upstream = FakeUpstream([RawResponse(events)], model="served-model")
    base_url = upstream.start()
    deps, ledger, client = _deps(tmp_path, base_url)
    try:
        response = relay(_request_body(True), {}, "trial-1", deps, ChatAdapter())
        iterator = response.chunks
        first = next(iterator)
        rows_after_first = _rows(tmp_path)
        rest = list(iterator)
    finally:
        ledger.close()
        client.close()
        upstream.stop()

    assert not [row for row in rows_after_first if row["type"] == "request_end"]
    assert b"".join([first, *rest]) == b"".join(events)
    end = _rows(tmp_path)[-1]
    assert (end["prompt_tokens"], end["completion_tokens"]) == (1, 2)
    assert end["completion_chars"] == 2


def test_stream_forwards_raw_upstream_bytes(tmp_path):
    raw_chunks = [
        b": keepalive\r\n\r\n",
        b'data: {"choices":[{"delta":{"content":"caf\xc3',
        b'\xa9"}}]}\r\n\r\n',
        b"data: [DONE]\r\n\r\n",
    ]
    upstream = FakeUpstream([RawResponse(raw_chunks)], model="served-model")
    base_url = upstream.start()
    deps, ledger, client = _deps(tmp_path, base_url)
    try:
        response = relay(_request_body(True), {}, "trial-1", deps, ChatAdapter())
        chunks = list(response.chunks)
    finally:
        ledger.close()
        client.close()
        upstream.stop()

    assert b"".join(chunks) == b"".join(raw_chunks)
    end = _rows(tmp_path)[-1]
    assert end["completion_chars"] == len("café")
    assert end["upstream_status"] == "ok"


def test_stream_parses_multi_data_line_events(tmp_path):
    event = (
        b'data: {"model":"joined-model","choices":[{"delta":{"content":"hi"}}],\r\n'
        b'data: "usage":{"prompt_tokens":7,"completion_tokens":2,"cached_read_tokens":1}}\r\n'
        b"\r\n"
    )
    upstream = FakeUpstream([RawResponse([event, b"data: [DONE]\r\n\r\n"])], model="served-model")
    base_url = upstream.start()
    deps, ledger, client = _deps(tmp_path, base_url)
    try:
        response = relay(_request_body(True), {}, "trial-1", deps, ChatAdapter())
        chunks = list(response.chunks)
    finally:
        ledger.close()
        client.close()
        upstream.stop()

    assert chunks[0] == event
    end = _rows(tmp_path)[-1]
    assert (end["prompt_tokens"], end["completion_tokens"], end["cached_read_tokens"]) == (
        7,
        2,
        1,
    )
    assert end["completion_chars"] == 2
    assert end["served_model"] == "joined-model"


def test_first_chunk_is_stamped_before_a_partial_event_completes(tmp_path):
    gate = Event()
    raw = RawResponse(
        chunks=[
            b'data: {"choices":[{"delta":{"content":"hel',
            b'lo"}}]}\n\n',
            b"data: [DONE]\n\n",
        ],
        gate=gate,
        gate_after=1,
    )
    upstream = FakeUpstream([raw], model="served-model")
    base_url = upstream.start()
    clock = _Clock([0.0, 5.0, 9.0, 12.0])
    deps, ledger, client = _deps(tmp_path, base_url, now=clock)
    response = None
    thread = None
    try:
        response = relay(_request_body(True), {}, "trial-1", deps, ChatAdapter())
        chunks = []

        def consume():
            chunks.extend(response.chunks)

        thread = Thread(target=consume, daemon=True)
        thread.start()
        assert clock.first_mark.wait(timeout=1.0)
        gate.set()
        thread.join(timeout=2)
        assert not thread.is_alive()
    finally:
        gate.set()
        raw.release.set()
        if thread is not None:
            thread.join(timeout=2)
        closer = getattr(response, "close", None)
        if closer is not None:
            closer()
        ledger.close()
        client.close()
        upstream.stop()

    end = _rows(tmp_path)[-1]
    assert end["first_chunk_s"] == 5.0
    assert end["first_content_s"] == 9.0
    assert end["duration_s"] == 12.0


def test_done_ends_the_stream_without_waiting_for_connection_close(tmp_path):
    raw = RawResponse(
        [
            _event({"choices": [{"delta": {"content": "hello"}}]}),
            b"data: [DONE]\n\n",
        ],
        hold_open=True,
    )
    upstream = FakeUpstream([raw], model="served-model")
    base_url = upstream.start()
    deps, ledger, client = _deps(tmp_path, base_url)
    response = relay(_request_body(True), {}, "trial-1", deps, ChatAdapter())
    chunks = []
    finished = Event()

    def consume():
        try:
            chunks.extend(response.chunks)
        finally:
            finished.set()

    thread = Thread(target=consume, daemon=True)
    thread.start()
    try:
        assert finished.wait(timeout=1.0)
    finally:
        raw.release.set()
        thread.join(timeout=2)
        closer = getattr(response, "close", None)
        if closer is not None:
            closer()
        ledger.close()
        client.close()
        upstream.stop()

    assert chunks[-1] == b"data: [DONE]\n\n"
    end = _rows(tmp_path)[-1]
    assert end["upstream_status"] == "ok"


def test_response_close_before_iteration_closes_upstream(tmp_path):
    raw = RawResponse([b": keepalive\r\n\r\n"], hold_open=True)
    upstream = FakeUpstream([raw], model="served-model")
    base_url = upstream.start()
    deps, ledger, client = _deps(tmp_path, base_url)
    try:
        response = relay(_request_body(True), {}, "trial-1", deps, ChatAdapter())
        assert raw.ready.wait(timeout=1.0)
        response.close()
        assert raw.disconnected.wait(timeout=1.0)
    finally:
        raw.release.set()
        ledger.close()
        client.close()
        upstream.stop()


def test_chunks_close_before_iteration_closes_upstream(tmp_path):
    raw = RawResponse([b": keepalive\r\n\r\n"], hold_open=True)
    upstream = FakeUpstream([raw], model="served-model")
    base_url = upstream.start()
    deps, ledger, client = _deps(tmp_path, base_url)
    try:
        response = relay(_request_body(True), {}, "trial-1", deps, ChatAdapter())
        assert raw.ready.wait(timeout=1.0)
        response.chunks.close()
        assert raw.disconnected.wait(timeout=1.0)
    finally:
        raw.release.set()
        ledger.close()
        client.close()
        upstream.stop()


def test_empty_non_stream_body_is_not_ok(tmp_path):
    upstream = FakeUpstream(
        [RawResponse([], content_type="application/json")], model="served-model"
    )
    base_url = upstream.start()
    deps, ledger, client = _deps(tmp_path, base_url)
    try:
        response = relay(_request_body(False), {}, "trial-1", deps, ChatAdapter())
        chunks = list(response.chunks)
    finally:
        ledger.close()
        client.close()
        upstream.stop()

    end = _rows(tmp_path)[-1]
    assert chunks == []
    assert end["upstream_status"] == "truncated"
    assert end["first_chunk_s"] is None
    assert end["first_content_s"] is None
    assert end["prompt_tokens"] is None
    assert end["completion_chars"] == 0


def test_unparseable_non_stream_body_is_not_ok(tmp_path):
    body = b"not json"
    upstream = FakeUpstream(
        [RawResponse([body], content_type="application/json")], model="served-model"
    )
    base_url = upstream.start()
    deps, ledger, client = _deps(tmp_path, base_url)
    try:
        response = relay(_request_body(False), {}, "trial-1", deps, ChatAdapter())
        chunks = list(response.chunks)
    finally:
        ledger.close()
        client.close()
        upstream.stop()

    end = _rows(tmp_path)[-1]
    assert chunks == [body]
    assert end["upstream_status"] == "truncated"
    assert end["first_chunk_s"] is not None
    assert end["completion_chars"] == 0
