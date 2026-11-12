from __future__ import annotations

import json
import queue
import re
import socket
import threading
import time
import uuid
from collections.abc import Callable, Generator, Iterator, Mapping
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, cast

import httpx

from harbor_research_runner.model_proxy.chars import (
    chat_completion_chars,
    chat_delta_chars,
    prompt_chars_of_payload,
)
from harbor_research_runner.model_proxy.ledger import LedgerWriter
from harbor_research_runner.model_proxy.status import (
    UpstreamOutcome,
    classify,
    retry_after_seconds,
    status_for_harness,
)
from harbor_research_runner.model_proxy.timing import StreamClock
from harbor_research_runner.model_proxy.usage import normalize_usage
from harbor_research_runner.records.limits import Limits
from harbor_research_runner.records.proxy_ledger import RequestEnd, RequestStart

_UNAVAILABLE_BODY = (
    b'{"error": {"message": "upstream unavailable", "type": "upstream_unavailable"}}'
)


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


@dataclass
class _Observation:
    http_status: int | None = None
    retry_after_s: float | None = None
    error_body: str = ""
    got_terminator: bool = False
    cut_by_proxy: str | None = None
    transport_failed: bool = False
    client_disconnected: bool = False
    served_model: str | None = None
    usage: dict[str, int] | None = None
    completion_chars: int = 0

    def outcome(self) -> UpstreamOutcome:
        return UpstreamOutcome(
            http_status=self.http_status,
            retry_after_s=self.retry_after_s,
            error_body=self.error_body,
            got_terminator=self.got_terminator,
            cut_by_proxy=self.cut_by_proxy,
            transport_failed=self.transport_failed,
            client_disconnected=self.client_disconnected,
        )


class _Cut(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class _Transport(Exception):
    pass


class _UpstreamBody:
    def __init__(self, response: httpx.Response, limits: Limits, started: float) -> None:
        self._response = response
        self._limits = limits
        self._started = started
        self._timing_lock = threading.Lock()
        self._waiting_since: float | None = None
        self._last_chunk_at: float | None = None
        self._queue: queue.Queue[tuple[str, object]] = queue.Queue(maxsize=16)
        self._ended = False
        self._closed = False
        self._finished = threading.Event()
        self._thread = threading.Thread(target=self._pump, daemon=True)
        self._thread.start()

    def _put(self, item: tuple[str, object]) -> bool:
        while not self._closed:
            try:
                self._queue.put(item, timeout=0.05)
                return True
            except queue.Full:
                continue
        return False

    def _pump(self) -> None:
        try:
            chunks = iter(self._response.iter_bytes())
            while not self._closed:
                with self._timing_lock:
                    self._waiting_since = time.monotonic()
                try:
                    chunk = next(chunks)
                except StopIteration:
                    break
                arrived = time.monotonic()
                with self._timing_lock:
                    self._waiting_since = None
                    if chunk:
                        self._last_chunk_at = arrived
                if self._closed:
                    return
                if chunk and not self._put(("chunk", (chunk, arrived))):
                    return
        except BaseException as exc:
            with self._timing_lock:
                self._waiting_since = None
            self._put(("error", exc))
        else:
            with self._timing_lock:
                self._waiting_since = None
            self._put(("end", None))
        finally:
            with self._timing_lock:
                self._waiting_since = None
            with suppress(Exception):
                self._response.close()
            self._finished.set()

    def _cut_reason(self, first_chunk: bool, now: float) -> str | None:
        if now - self._started >= self._limits.request_latency_max_s:
            return "latency"
        with self._timing_lock:
            waiting_since = self._waiting_since
            last_chunk_at = self._last_chunk_at
        if (
            last_chunk_at is None
            and first_chunk
            and now - self._started >= self._limits.first_chunk_max_s
        ):
            return "first_chunk"
        if (
            last_chunk_at is not None
            and waiting_since is not None
            and now - waiting_since >= self._limits.inter_chunk_gap_max_s
        ):
            return "gap"
        return None

    def next(self, *, first_chunk: bool) -> bytes | None:
        if self._ended:
            return None
        while True:
            now = time.monotonic()
            if now - self._started >= self._limits.request_latency_max_s:
                raise _Cut("latency")
            try:
                kind, value = self._queue.get_nowait()
            except queue.Empty:
                reason = self._cut_reason(first_chunk, now)
                if reason is not None:
                    raise _Cut(reason) from None
                try:
                    kind, value = self._queue.get(timeout=0.05)
                except queue.Empty:
                    continue
            if time.monotonic() - self._started >= self._limits.request_latency_max_s:
                raise _Cut("latency")
            if kind == "error":
                self._ended = True
                raise _Transport from None
            if kind == "end":
                self._ended = True
                return None
            chunk, _arrived = cast(tuple[bytes, float], value)
            return chunk

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if not self._finished.is_set():
            network_stream = self._response.extensions.get("network_stream")
            raw_socket = (
                network_stream.get_extra_info("socket") if network_stream is not None else None
            )
            if raw_socket is not None:
                with suppress(OSError, ValueError):
                    raw_socket.shutdown(socket.SHUT_RDWR)
                self._thread.join(timeout=0.2)
            else:
                with suppress(Exception):
                    self._response.close()


class _RelayState:
    def __init__(
        self,
        deps: RelayDeps,
        trial: str,
        request_id: str,
        clock: StreamClock,
        observation: _Observation,
        body: _UpstreamBody | None,
    ) -> None:
        self.deps = deps
        self.trial = trial
        self.request_id = request_id
        self.clock = clock
        self.observation = observation
        self.body = body
        self.header_status: int | None = None
        self._written = False
        self._http_status: int | None = None

    def finish(self, http_status: int | None = None) -> int:
        if self._written:
            assert self._http_status is not None
            return self._http_status
        upstream_status = classify(self.observation.outcome())
        if http_status is None:
            http_status = status_for_harness(upstream_status, self.observation.http_status)
        self._http_status = http_status
        self._written = True
        tokens = self.observation.usage or {}
        self.deps.ledger.write(
            RequestEnd(
                0,
                0.0,
                self.trial,
                self.request_id,
                http_status,
                upstream_status,
                self.observation.http_status,
                self.observation.served_model,
                tokens.get("prompt_tokens"),
                tokens.get("completion_tokens"),
                tokens.get("cached_read_tokens"),
                self.observation.completion_chars,
                self.clock.first_chunk_s,
                self.clock.first_content_s,
                self.clock.finish(),
                0,
                self.observation.error_body[:500] or None,
            )
        )
        return http_status

    def client_disconnect(self) -> None:
        if self._written:
            return
        self.observation.client_disconnected = True
        self.finish(self.header_status)

    def close_body(self) -> None:
        if self.body is not None:
            self.body.close()


class _OwnedChunks:
    def __init__(self, source: Generator[bytes, None, None], state: _RelayState) -> None:
        self._source = source
        self._state = state
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
        self._state.client_disconnect()
        try:
            self._source.close()
        finally:
            self._state.close_body()


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
        "POST",
        route.endpoint,
        headers=upstream_headers,
        json=outbound,
        timeout=_header_timeout(
            deps.client,
            deps.limits.first_chunk_max_s,
            deps.limits.request_latency_max_s,
        ),
    )
    observation = _Observation()
    started = time.monotonic()
    try:
        response = deps.client.send(request, stream=True)
    except httpx.TimeoutException as exc:
        if isinstance(exc, httpx.ReadTimeout):
            observation.cut_by_proxy = (
                "latency"
                if deps.limits.request_latency_max_s <= deps.limits.first_chunk_max_s
                else "first_chunk"
            )
        else:
            observation.transport_failed = True
        state = _RelayState(deps, trial, request_id, clock, observation, None)
        status = state.finish()
        return RelayResponse(status, _json_headers(), iter([_UNAVAILABLE_BODY]))
    except httpx.TransportError:
        observation.transport_failed = True
        state = _RelayState(deps, trial, request_id, clock, observation, None)
        status = state.finish()
        return RelayResponse(status, _json_headers(), iter([_UNAVAILABLE_BODY]))

    request.extensions.setdefault("timeout", {})["read"] = None
    observation.http_status = response.status_code
    observation.retry_after_s = retry_after_seconds(response.headers.get("Retry-After"))
    response_headers = _response_headers(response)
    body = _UpstreamBody(response, deps.limits, started)
    state = _RelayState(deps, trial, request_id, clock, observation, body)

    if response.status_code != 200:
        return _error_response(state, response.charset_encoding or "utf-8", response_headers)

    try:
        first = body.next(first_chunk=True)
    except _Cut as cut:
        observation.cut_by_proxy = cut.reason
        state.close_body()
        status = state.finish()
        return RelayResponse(status, _json_headers(), iter([_UNAVAILABLE_BODY]))
    except _Transport:
        observation.transport_failed = True
        state.close_body()
        status = state.finish()
        return RelayResponse(status, _json_headers(), iter([_UNAVAILABLE_BODY]))

    if first is not None:
        clock.mark_chunk()

    if not is_stream:
        state.header_status = 200
        return RelayResponse(
            200, response_headers, _OwnedChunks(_nonstream_chunks(state, first), state)
        )

    if first is None:
        state.header_status = 200
        state.close_body()
        status = state.finish(200)
        return RelayResponse(status, response_headers, iter([]))

    state.header_status = 200
    return RelayResponse(200, response_headers, _OwnedChunks(_stream_chunks(state, first), state))


def _error_response(state: _RelayState, charset: str, headers: dict[str, str]) -> RelayResponse:
    observation = state.observation
    assert state.body is not None
    try:
        body = _collect(state.body, state.clock, first_chunk=True)
    except _Cut as cut:
        observation.cut_by_proxy = cut.reason
        state.close_body()
        status = state.finish()
        return RelayResponse(status, _json_headers(), iter([_UNAVAILABLE_BODY]))
    except _Transport:
        observation.transport_failed = True
        state.close_body()
        status = state.finish()
        return RelayResponse(status, _json_headers(), iter([_UNAVAILABLE_BODY]))
    observation.error_body = body.decode(charset, errors="replace")[:500]
    state.close_body()
    status = state.finish()
    return RelayResponse(status, headers, iter([body] if body else []))


def _nonstream_chunks(state: _RelayState, first: bytes | None) -> Generator[bytes, None, None]:
    observation = state.observation
    assert state.body is not None
    try:
        try:
            body = _collect_with_first(state.body, state.clock, first)
        except _Cut as cut:
            observation.cut_by_proxy = cut.reason
            return
        except _Transport:
            observation.transport_failed = True
            return
        obj = _json_object(body)
        observation.usage = normalize_usage(obj)
        observation.completion_chars = chat_completion_chars(obj)
        if observation.completion_chars:
            state.clock.mark_content()
        observation.served_model = _served_model_of(obj)
        observation.got_terminator = obj is not None
        state.finish(state.header_status)
        state.close_body()
        if body:
            yield body
    finally:
        state.finish(state.header_status)
        state.close_body()


def _collect(body: _UpstreamBody, clock: StreamClock, *, first_chunk: bool) -> bytes:
    chunks: list[bytes] = []
    chunk = body.next(first_chunk=first_chunk)
    while chunk is not None:
        clock.mark_chunk()
        chunks.append(chunk)
        chunk = body.next(first_chunk=False)
    return b"".join(chunks)


def _collect_with_first(body: _UpstreamBody, clock: StreamClock, first: bytes | None) -> bytes:
    chunks: list[bytes] = []
    if first is None:
        return b""
    chunks.append(first)
    while True:
        chunk = body.next(first_chunk=False)
        if chunk is None:
            return b"".join(chunks)
        clock.mark_chunk()
        chunks.append(chunk)


def _stream_chunks(state: _RelayState, first: bytes) -> Generator[bytes, None, None]:
    observation = state.observation
    assert state.body is not None
    tail: list[bytes] = []
    aborted = False
    try:
        try:
            for raw, obj, done in _iter_events(_chunk_stream(state.body, first), state.clock):
                found_usage, found_chars, found_model = _observe(obj, state.clock)
                if found_usage is not None:
                    observation.usage = found_usage
                if found_model is not None:
                    observation.served_model = found_model
                observation.completion_chars += found_chars
                if tail or done or _is_terminal(obj):
                    tail.append(raw)
                    if done:
                        observation.got_terminator = True
                        break
                    continue
                yield raw
        except _Cut as cut:
            observation.cut_by_proxy = cut.reason
            aborted = True
        except _Transport:
            observation.transport_failed = True
            aborted = True
        finally:
            state.finish(state.header_status)
            state.close_body()
        if tail and not aborted:
            for raw in tail:
                yield raw
    finally:
        state.finish(state.header_status)
        state.close_body()


def _chunk_stream(body: _UpstreamBody, first: bytes) -> Iterator[bytes]:
    yield first
    while True:
        chunk = body.next(first_chunk=False)
        if chunk is None:
            return
        yield chunk


def _response_headers(response: httpx.Response) -> dict[str, str]:
    headers = {"Content-Type": response.headers.get("content-type", "application/json")}
    retry_after = response.headers.get("Retry-After")
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    return headers


def _json_headers() -> dict[str, str]:
    return {"Content-Type": "application/json"}


def _header_timeout(
    client: httpx.Client, first_chunk_max_s: float, request_latency_max_s: float
) -> httpx.Timeout:
    base = client.timeout
    if isinstance(base, httpx.Timeout):
        values = base.as_dict()
    else:
        values = {"connect": None, "read": None, "write": None, "pool": None}
    values["read"] = min(first_chunk_max_s, request_latency_max_s)
    return httpx.Timeout(**values)


_EVENT_SEPARATOR = re.compile(rb"\r\n\r\n|\n\n|\r\r")


def _iter_events(
    chunks: Iterator[bytes], clock: StreamClock
) -> Iterator[tuple[bytes, dict | None, bool]]:
    buffer = b""
    for chunk in chunks:
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
