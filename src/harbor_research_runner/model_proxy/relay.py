from __future__ import annotations

import json
import re
import time
import uuid
from collections.abc import Callable, Generator, Iterator, Mapping
from dataclasses import dataclass
from typing import Any

import httpx

from harbor_research_runner.model_proxy.chars import (
    chat_completion_chars,
    chat_delta_chars,
    prompt_chars_of_payload,
)
from harbor_research_runner.model_proxy.ledger import LedgerWriter
from harbor_research_runner.model_proxy.timing import StreamClock
from harbor_research_runner.model_proxy.usage import normalize_usage
from harbor_research_runner.records.limits import Limits
from harbor_research_runner.records.proxy_ledger import RequestEnd, RequestStart


@dataclass(frozen=True)
class Route:
    endpoint: str
    key: str | None


@dataclass
class RelayDeps:
    ledger: LedgerWriter
    model: dict
    limits: Limits
    client: httpx.Client
    routes: Callable[[], Iterator[Route]]
    now: Callable[[], float] = time.monotonic


@dataclass
class RelayResponse:
    http_status: int
    headers: dict[str, str]
    chunks: Iterator[bytes]

    def close(self) -> None:
        closer = getattr(self.chunks, "close", None)
        if closer is not None:
            closer()


class _OwnedChunks:
    def __init__(self, source: Generator[bytes, None, None], response: httpx.Response) -> None:
        self._source = source
        self._response = response
        self._closed = False

    def __iter__(self) -> _OwnedChunks:
        return self

    def __next__(self) -> bytes:
        if self._closed:
            raise StopIteration
        try:
            return next(self._source)
        except StopIteration:
            self.close()
            raise
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._source.close()
        finally:
            self._response.close()


def relay_chat(
    request_body: bytes,
    headers: Mapping[str, str],
    trial: str,
    deps: RelayDeps,
) -> RelayResponse:
    """Forward one chat-completions request and record its ledger rows."""
    payload = json.loads(request_body)
    if not isinstance(payload, dict):
        raise ValueError("request body must be a JSON object")
    is_stream = bool(payload.get("stream"))
    requested_model = payload.get("model")
    model = requested_model if isinstance(requested_model, str) else deps.model["id"]
    outbound = {**payload, "model": deps.model["upstream_model"]}
    request_id = uuid.uuid4().hex
    clock = StreamClock(deps.now)

    deps.ledger.write(
        RequestStart(
            0,
            0.0,
            trial,
            request_id,
            model,
            is_stream,
            prompt_chars_of_payload(outbound),
            0.0,
        )
    )

    route = next(deps.routes())
    upstream_headers = {"content-type": "application/json"}
    if route.key is not None:
        upstream_headers["authorization"] = f"Bearer {route.key}"
    request = deps.client.build_request(
        "POST", route.endpoint, headers=upstream_headers, json=outbound
    )
    response = deps.client.send(request, stream=True)
    content_type = response.headers.get("content-type", "application/json")
    chunks = _OwnedChunks(_chunks(response, deps, trial, request_id, clock, is_stream), response)
    return RelayResponse(
        http_status=response.status_code,
        headers={"Content-Type": content_type},
        chunks=chunks,
    )


def _read_body(response: httpx.Response, clock: StreamClock) -> bytes:
    body = bytearray()
    for chunk in response.iter_bytes():
        if not chunk:
            continue
        clock.mark_chunk()
        body.extend(chunk)
    return bytes(body)


def _chunks(
    response: httpx.Response,
    deps: RelayDeps,
    trial: str,
    request_id: str,
    clock: StreamClock,
    is_stream: bool,
) -> Generator[bytes, None, None]:
    try:
        if response.status_code != 200:
            body = _read_body(response, clock)
            _write_end(
                deps,
                trial,
                request_id,
                response.status_code,
                "server_error",
                None,
                None,
                0,
                clock,
            )
            if body:
                yield body
            return
        if not is_stream:
            body = _read_body(response, clock)
            obj = _json_object(body)
            usage = normalize_usage(obj)
            completion_chars = chat_completion_chars(obj)
            if completion_chars:
                clock.mark_content()
            _write_end(
                deps,
                trial,
                request_id,
                response.status_code,
                "ok" if obj is not None else "server_error",
                _served_model_of(obj),
                usage,
                completion_chars,
                clock,
            )
            if body:
                yield body
            return
        yield from _stream_chunks(response, deps, trial, request_id, clock)
    finally:
        response.close()


_EVENT_SEPARATOR = re.compile(rb"\r\n\r\n|\n\n|\r\r")


def _stream_chunks(
    response: httpx.Response,
    deps: RelayDeps,
    trial: str,
    request_id: str,
    clock: StreamClock,
) -> Iterator[bytes]:
    tail: list[bytes] = []
    usage: dict[str, int] | None = None
    served_model: str | None = None
    completion_chars = 0
    terminal = False
    terminated = False

    for raw, obj, done in _iter_events(response, clock):
        found_usage, found_chars, found_model = _observe(obj, clock)
        if found_usage is not None:
            usage = found_usage
        if found_model is not None:
            served_model = found_model
        completion_chars += found_chars
        if terminal or done or _is_terminal(obj):
            terminal = True
            tail.append(raw)
            if done:
                terminated = True
                break
            continue
        yield raw

    if tail:
        _write_end(
            deps,
            trial,
            request_id,
            response.status_code,
            "ok" if terminated else "server_error",
            served_model,
            usage,
            completion_chars,
            clock,
        )
        response.close()
        for raw in tail:
            yield raw
        return

    _write_end(
        deps,
        trial,
        request_id,
        response.status_code,
        "server_error",
        served_model,
        usage,
        completion_chars,
        clock,
    )
    response.close()


def _iter_events(
    response: httpx.Response, clock: StreamClock
) -> Iterator[tuple[bytes, dict | None, bool]]:
    buffer = b""
    for chunk in response.iter_bytes():
        if not chunk:
            continue
        clock.mark_chunk()
        buffer += chunk
        while True:
            match = _EVENT_SEPARATOR.search(buffer)
            if match is None:
                break
            raw = buffer[: match.end()]
            buffer = buffer[match.end() :]
            yield _parse_event(raw)
    if buffer:
        yield _parse_event(buffer)


def _parse_event(raw: bytes) -> tuple[bytes, dict | None, bool]:
    data_lines: list[str] = []
    for line in raw.decode("utf-8", "replace").splitlines():
        if not line.startswith("data:"):
            continue
        value = line[5:]
        if value.startswith(" "):
            value = value[1:]
        data_lines.append(value)
    if not data_lines:
        return raw, None, False
    payload = "\n".join(data_lines)
    if payload.strip() == "[DONE]":
        return raw, None, True
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError:
        return raw, None, False
    return raw, parsed if isinstance(parsed, dict) else None, False


def _is_terminal(obj: Any) -> bool:
    if not isinstance(obj, dict):
        return False
    choices = obj.get("choices")
    if isinstance(choices, list):
        for choice in choices:
            if isinstance(choice, dict) and choice.get("finish_reason") is not None:
                return True
        if choices:
            return False
    return normalize_usage(obj) is not None


def _observe(obj: Any, clock: StreamClock) -> tuple[dict[str, int] | None, int, str | None]:
    if obj is None:
        return None, 0, None
    completion_chars = chat_delta_chars(obj)
    if completion_chars:
        clock.mark_content()
    return normalize_usage(obj), completion_chars, _served_model_of(obj)


def _served_model_of(obj: Any) -> str | None:
    if not isinstance(obj, dict):
        return None
    model = obj.get("model")
    if isinstance(model, str):
        return model
    response = obj.get("response")
    if isinstance(response, dict):
        model = response.get("model")
        if isinstance(model, str):
            return model
    return None


def _json_object(body: bytes) -> dict | None:
    try:
        value = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _write_end(
    deps: RelayDeps,
    trial: str,
    request_id: str,
    http_status: int,
    upstream_status: str,
    served_model: str | None,
    usage: dict[str, int] | None,
    completion_chars: int,
    clock: StreamClock,
) -> None:
    tokens = usage or {}
    deps.ledger.write(
        RequestEnd(
            0,
            0.0,
            trial,
            request_id,
            http_status,
            upstream_status,
            http_status,
            served_model,
            tokens.get("prompt_tokens"),
            tokens.get("completion_tokens"),
            tokens.get("cached_read_tokens"),
            completion_chars,
            clock.first_chunk_s,
            clock.first_content_s,
            clock.finish(),
            0,
            None,
        )
    )
