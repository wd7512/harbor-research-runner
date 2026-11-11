from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from http.client import RemoteDisconnected
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from tests.support.fake_upstream import FakeUpstream


def _request(script: str) -> tuple[int, bytes]:
    upstream = FakeUpstream([script])
    base_url = upstream.start()
    try:
        request = Request(
            f"{base_url}/v1/chat/completions",
            data=json.dumps({"model": "fake-model", "stream": True}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=2) as response:
                return response.status, response.read()
        except HTTPError as error:
            return error.code, error.read()
    finally:
        upstream.stop()


def _frames(body: bytes) -> list[bytes]:
    return [line[6:] for line in body.splitlines() if line.startswith(b"data: ")]


def test_ok_sends_five_chunks_usage_and_terminator() -> None:
    status, body = _request("ok")

    assert status == 200
    frames = _frames(body)
    assert len(frames) == 7
    assert [json.loads(frame)["choices"][0]["finish_reason"] for frame in frames[:4]] == [
        None,
        None,
        None,
        None,
    ]
    assert json.loads(frames[4])["choices"][0]["finish_reason"] == "stop"
    assert json.loads(frames[-2])["usage"] == {
        "prompt_tokens": 100,
        "completion_tokens": 20,
        "total_tokens": 120,
        "prompt_tokens_details": {"cached_tokens": 0},
    }
    assert frames[-1] == b"[DONE]"


def test_ok_token_count_uses_the_requested_completion_usage() -> None:
    status, body = _request("ok:7")

    assert status == 200
    frames = _frames(body)
    assert len(frames) == 7
    assert json.loads(frames[-2])["usage"]["completion_tokens"] == 7
    assert frames[-1] == b"[DONE]"


def test_http_code_sends_json_error_without_stream_frames() -> None:
    status, body = _request("http_503")

    assert status == 503
    assert json.loads(body)["error"]
    assert _frames(body) == []


def test_rate_limit_sends_retry_after_error() -> None:
    upstream = FakeUpstream(["rate_limit"])
    base_url = upstream.start()
    try:
        request = Request(
            f"{base_url}/v1/chat/completions",
            data=b'{"stream":true}',
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(HTTPError) as caught:
            urlopen(request, timeout=2)
        assert caught.value.code == 429
        assert caught.value.headers["Retry-After"] == "1"
        assert _frames(caught.value.read()) == []
    finally:
        upstream.stop()


def test_quota_sends_long_retry_after_error() -> None:
    upstream = FakeUpstream(["quota"])
    base_url = upstream.start()
    try:
        request = Request(
            f"{base_url}/v1/chat/completions",
            data=b'{"stream":true}',
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(HTTPError) as caught:
            urlopen(request, timeout=2)
        assert caught.value.code == 429
        assert caught.value.headers["Retry-After"] == "86400"
        assert b"quota" in caught.value.read()
    finally:
        upstream.stop()


def test_stall_closes_without_sending_chunks() -> None:
    upstream = FakeUpstream(["stall:0.01"])
    base_url = upstream.start()
    request = Request(
        f"{base_url}/v1/chat/completions",
        data=b'{"stream":true}',
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    started = time.monotonic()
    try:
        with pytest.raises(RemoteDisconnected):
            urlopen(request, timeout=2)
    finally:
        upstream.stop()

    assert time.monotonic() - started >= 0.01


def test_gap_sends_two_chunks_then_finishes() -> None:
    status, body = _request("gap:0.01")

    assert status == 200
    frames = _frames(body)
    assert len(frames) == 7
    assert frames[-1] == b"[DONE]"


def test_truncate_after_closes_without_terminator() -> None:
    status, body = _request("truncate_after:3")

    assert status == 200
    assert len(_frames(body)) == 3
    assert b"[DONE]" not in body
    assert all(json.loads(frame)["choices"][0]["finish_reason"] is None for frame in _frames(body))


def test_drop_closes_connection() -> None:
    upstream = FakeUpstream(["drop"])
    base_url = upstream.start()
    try:
        request = Request(
            f"{base_url}/v1/chat/completions",
            data=b'{"stream":true}',
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(RemoteDisconnected):
            urlopen(request, timeout=2)
    finally:
        upstream.stop()


def test_model_entry_changes_response_model() -> None:
    status, body = _request("model:other-model")

    assert status == 200
    assert json.loads(_frames(body)[0])["model"] == "other-model"
    assert _frames(body)[-1] == b"[DONE]"


def test_slow_entry_paces_completion_chunks() -> None:
    upstream = FakeUpstream(["slow:200"])
    base_url = upstream.start()
    request = Request(
        f"{base_url}/v1/chat/completions",
        data=b'{"stream":true}',
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    started = time.monotonic()
    try:
        with urlopen(request, timeout=2) as response:
            status = response.status
            body = response.read()
    finally:
        upstream.stop()
    elapsed = time.monotonic() - started

    assert status == 200
    assert len(_frames(body)) == 7
    assert _frames(body)[-1] == b"[DONE]"
    assert 0.1 <= elapsed < 1


def test_request_capture_keeps_path_headers_and_parsed_body() -> None:
    upstream = FakeUpstream(["ok"])
    base_url = upstream.start()
    try:
        request = Request(
            f"{base_url}/v1/chat/completions",
            data=b'{"model":"capture-model","stream":true}',
            headers={"Content-Type": "application/json", "X-Caller": "integration"},
            method="POST",
        )
        with urlopen(request, timeout=2) as response:
            response.read()
    finally:
        upstream.stop()

    assert len(upstream.requests) == 1
    captured = upstream.requests[0]
    assert captured["path"] == "/v1/chat/completions"
    assert captured["headers"]["x-caller"] == "integration"
    assert captured["body"] == {"model": "capture-model", "stream": True}


def test_responses_stream_sends_one_completed_event_with_usage() -> None:
    upstream = FakeUpstream(["ok"])
    base_url = upstream.start()
    request = Request(
        f"{base_url}/v1/responses",
        data=b'{"model":"fake-model","stream":true}',
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=2) as response:
            status = response.status
            body = response.read()
    finally:
        upstream.stop()

    blocks = [block for block in body.decode().split("\n\n") if block]
    events = [
        (block.splitlines()[0][7:], json.loads(block.split("data: ", 1)[1])) for block in blocks
    ]
    assert status == 200
    assert [event for event, _ in events] == [
        "response.created",
        "response.output_text.delta",
        "response.output_text.delta",
        "response.output_text.delta",
        "response.output_text.delta",
        "response.output_text.delta",
        "response.completed",
    ]
    assert events[0][1] == {
        "type": "response.created",
        "response": {
            "id": "resp_fake",
            "object": "response",
            "model": "fake-model",
            "status": "in_progress",
        },
    }
    assert events[-1][1]["response"]["usage"]["output_tokens"] == 20


def test_responses_nonstream_ok_returns_complete_response_json() -> None:
    upstream = FakeUpstream(["ok"])
    base_url = upstream.start()
    request = Request(
        f"{base_url}/v1/responses",
        data=b'{"model":"fake-model"}',
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=2) as response:
            status = response.status
            body = json.load(response)
    finally:
        upstream.stop()

    assert status == 200
    assert body["status"] == "completed"
    assert body["usage"]["input_tokens"] == 100
    assert body["usage"]["output_tokens"] == 20


def test_responses_truncate_after_two_deltas_omits_completion() -> None:
    upstream = FakeUpstream(["truncate_after:2"])
    base_url = upstream.start()
    request = Request(
        f"{base_url}/v1/responses",
        data=b'{"model":"fake-model","stream":true}',
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=2) as response:
            status = response.status
            body = response.read()
    finally:
        upstream.stop()

    blocks = [block for block in body.decode().split("\n\n") if block]
    events = [block.splitlines()[0][7:] for block in blocks]
    assert status == 200
    assert events == [
        "response.created",
        "response.output_text.delta",
        "response.output_text.delta",
    ]


def test_concurrent_requests_use_script_entries_once_in_order() -> None:
    upstream = FakeUpstream(["ok", "http_500"])
    base_url = upstream.start()

    def send_request(_: int) -> int:
        request = Request(
            f"{base_url}/v1/chat/completions",
            data=b'{"stream":false}',
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=2) as response:
                response.read()
                return response.status
        except HTTPError as error:
            error.read()
            return error.code

    try:
        with ThreadPoolExecutor(max_workers=20) as executor:
            statuses = list(executor.map(send_request, range(20)))
    finally:
        upstream.stop()

    assert statuses.count(200) == 1
    assert statuses.count(500) == 19
    assert len(upstream.requests) == 20


def test_empty_script_is_rejected_at_construction() -> None:
    with pytest.raises(ValueError, match="empty script"):
        FakeUpstream([])


def test_unknown_script_entry_is_rejected_at_construction() -> None:
    with pytest.raises(ValueError, match="unknown entry: nonsense"):
        FakeUpstream(["ok", "nonsense"])
