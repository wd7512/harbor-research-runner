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
from dataclasses import dataclass, field
from typing import cast

import httpx

from harbor_research_runner.model_proxy.adapters.base import (
    UpstreamAdapter,
    UpstreamDecodeError,
)
from harbor_research_runner.model_proxy.bodies import BodyStore
from harbor_research_runner.model_proxy.chars import (
    prompt_chars_of_payload,
)
from harbor_research_runner.model_proxy.ledger import LedgerWriter
from harbor_research_runner.model_proxy.pacing import Pacer, estimate_request_tokens
from harbor_research_runner.model_proxy.status import (
    UpstreamOutcome,
    classify,
    retry_after_seconds,
    status_for_harness,
)
from harbor_research_runner.model_proxy.timing import StreamClock
from harbor_research_runner.records.limits import Limits
from harbor_research_runner.records.proxy_ledger import RequestEnd, RequestStart

_UNAVAILABLE_BODY = (
    b'{"error": {"message": "upstream unavailable", "type": "upstream_unavailable"}}'
)


@dataclass(frozen=True)
class Route:
    endpoint: str
    key: str | None = field(repr=False)


@dataclass
class RelayDeps:
    ledger: LedgerWriter
    model: dict
    limits: Limits
    client: httpx.Client
    routes: Callable[[], Iterator[Route]]
    now: Callable[[], float] = time.monotonic
    pacer: Pacer | None = None
    bodies: BodyStore | None = None


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
        failovers: int,
        estimated_tokens: int,
        request_body: bytes,
    ) -> None:
        self.deps = deps
        self.trial = trial
        self.request_id = request_id
        self.clock = clock
        self.observation = observation
        self.body = body
        self.failovers = failovers
        self.estimated_tokens = estimated_tokens
        self.request_body = request_body
        self.header_status: int | None = None
        self._written = False
        self._ledger_written = False
        self._pacer_recorded = False
        self._bodies_written = False
        self._http_status: int | None = None
        self._response_bytes = bytearray()

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
        try:
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
                    self.failovers,
                    self.observation.error_body[:500] or None,
                )
            )
            self._ledger_written = True
        finally:
            if self.deps.pacer is not None and not self._pacer_recorded:
                self._pacer_recorded = True
                usage = self.observation.usage
                actual = self.estimated_tokens
                if usage is not None:
                    actual = usage.get("prompt_tokens", 0) + usage.get("completion_tokens", 0)
                self.deps.pacer.record(actual)
        return http_status

    def client_disconnect(self) -> None:
        if self._written:
            return
        self.observation.client_disconnected = True
        self.finish(self.header_status)

    def close_body(self) -> None:
        if self.body is not None:
            self.body.close()

    def note_response_bytes(self, chunk: bytes) -> None:
        self._response_bytes.extend(chunk)

    def store_bodies(self) -> None:
        if self.deps.bodies is None or not self._ledger_written or self._bodies_written:
            return
        self._bodies_written = True
        self.deps.bodies.write(
            self.trial,
            self.request_id,
            self.request_body,
            bytes(self._response_bytes),
        )


class _OwnedChunks:
    def __init__(self, source: Iterator[bytes], state: _RelayState) -> None:
        self._source = source
        self._state = state
        self._closed = False

    def __iter__(self) -> _OwnedChunks:
        return self

    def __next__(self) -> bytes:
        if self._closed:
            raise StopIteration
        try:
            chunk = next(self._source)
            self._state.note_response_bytes(chunk)
            return chunk
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
            self._state.client_disconnect()
            closer = getattr(self._source, "close", None)
            if closer is not None:
                closer()
        finally:
            self._state.close_body()
            self._state.store_bodies()


def relay(
    request_body: bytes,
    headers: Mapping[str, str],
    trial: str,
    deps: RelayDeps,
    adapter: UpstreamAdapter,
) -> RelayResponse:
    """Forward one adapted model request and record its ledger rows."""
    payload = json.loads(request_body)
    if not isinstance(payload, dict):
        raise ValueError("request body must be a JSON object")
    is_stream = bool(payload.get("stream"))
    requested_model = payload.get("model")
    model = requested_model if isinstance(requested_model, str) else deps.model["id"]
    outbound, _warnings = adapter.build_request(payload, deps.model["upstream_model"])
    estimated_tokens = estimate_request_tokens(outbound, deps.model.get("max_output_tokens"))
    paced_wait_s = 0.0
    if deps.pacer is not None:
        paced_wait_s = deps.pacer.wait_for(estimated_tokens)
    request_id = uuid.uuid4().hex
    clock = StreamClock(deps.now)

    try:
        deps.ledger.write(
            RequestStart(
                0,
                0.0,
                trial,
                request_id,
                model,
                is_stream,
                prompt_chars_of_payload(outbound),
                paced_wait_s,
            )
        )
    except BaseException:
        if deps.pacer is not None:
            deps.pacer.record(estimated_tokens)
        raise

    routes = list(deps.routes())
    if not routes:
        observation = _Observation(transport_failed=True)
        state = _RelayState(
            deps,
            trial,
            request_id,
            clock,
            observation,
            None,
            0,
            estimated_tokens,
            request_body,
        )
        status = state.finish()
        return RelayResponse(
            status,
            _json_headers(),
            _OwnedChunks(iter([_UNAVAILABLE_BODY]), state),
        )
    for failovers, route in enumerate(routes):
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
            state = _RelayState(
                deps,
                trial,
                request_id,
                clock,
                observation,
                None,
                failovers,
                estimated_tokens,
                request_body,
            )
            if _can_fail_over(observation) and failovers + 1 < len(routes):
                continue
            status = state.finish()
            return RelayResponse(
                status,
                _json_headers(),
                _OwnedChunks(iter([_UNAVAILABLE_BODY]), state),
            )
        except httpx.TransportError:
            observation.transport_failed = True
            state = _RelayState(
                deps,
                trial,
                request_id,
                clock,
                observation,
                None,
                failovers,
                estimated_tokens,
                request_body,
            )
            if _can_fail_over(observation) and failovers + 1 < len(routes):
                continue
            status = state.finish()
            return RelayResponse(
                status,
                _json_headers(),
                _OwnedChunks(iter([_UNAVAILABLE_BODY]), state),
            )

        request.extensions.setdefault("timeout", {})["read"] = None
        observation.http_status = response.status_code
        observation.retry_after_s = retry_after_seconds(response.headers.get("Retry-After"))
        response_headers = _response_headers(response)
        body = _UpstreamBody(response, deps.limits, started)
        state = _RelayState(
            deps,
            trial,
            request_id,
            clock,
            observation,
            body,
            failovers,
            estimated_tokens,
            request_body,
        )

        if response.status_code != 200:
            error_response = _error_response(
                state,
                response.charset_encoding or "utf-8",
                response_headers,
                can_retry=failovers + 1 < len(routes),
            )
            if error_response is None:
                continue
            return error_response

        try:
            first = body.next(first_chunk=True)
        except _Cut as cut:
            observation.cut_by_proxy = cut.reason
            state.close_body()
            status = state.finish()
            return RelayResponse(
                status,
                _json_headers(),
                _OwnedChunks(iter([_UNAVAILABLE_BODY]), state),
            )
        except _Transport:
            observation.transport_failed = True
            state.close_body()
            if _can_fail_over(observation) and failovers + 1 < len(routes):
                continue
            status = state.finish()
            return RelayResponse(
                status,
                _json_headers(),
                _OwnedChunks(iter([_UNAVAILABLE_BODY]), state),
            )

        if first is not None:
            clock.mark_chunk()

        if not is_stream:
            state.header_status = 200
            return RelayResponse(
                200,
                response_headers,
                _OwnedChunks(
                    _nonstream_chunks(
                        state,
                        first,
                        adapter,
                        response_headers["Content-Type"],
                    ),
                    state,
                ),
            )

        if first is None:
            state.header_status = 200
            state.close_body()
            status = state.finish(200)
            return RelayResponse(status, response_headers, _OwnedChunks(iter([]), state))

        state.header_status = 200
        return RelayResponse(
            200,
            response_headers,
            _OwnedChunks(_stream_chunks(state, first, adapter), state),
        )

    raise StopIteration("no upstream routes available")


def _error_response(
    state: _RelayState,
    charset: str,
    headers: dict[str, str],
    *,
    can_retry: bool,
) -> RelayResponse | None:
    observation = state.observation
    assert state.body is not None
    try:
        body = _collect(state.body, state.clock, first_chunk=True)
    except _Cut as cut:
        observation.cut_by_proxy = cut.reason
        state.close_body()
        status = state.finish()
        return RelayResponse(
            status,
            _json_headers(),
            _OwnedChunks(iter([_UNAVAILABLE_BODY]), state),
        )
    except _Transport:
        observation.transport_failed = True
        state.close_body()
        if can_retry and _can_fail_over(observation):
            return None
        status = state.finish()
        return RelayResponse(
            status,
            _json_headers(),
            _OwnedChunks(iter([_UNAVAILABLE_BODY]), state),
        )
    observation.error_body = body.decode(charset, errors="replace")[:500]
    state.close_body()
    if can_retry and _can_fail_over(observation):
        return None
    status = state.finish()
    return RelayResponse(
        status,
        headers,
        _OwnedChunks(iter([body] if body else []), state),
    )


def _can_fail_over(observation: _Observation) -> bool:
    return classify(observation.outcome()) in {
        "rate_limited",
        "server_error",
        "transport_error",
    }


def _nonstream_chunks(
    state: _RelayState,
    first: bytes | None,
    adapter: UpstreamAdapter,
    content_type: str,
) -> Generator[bytes, None, None]:
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
        try:
            decoded = adapter.decode_response(
                body, state.deps.model["upstream_model"], content_type
            )
        except UpstreamDecodeError as exc:
            observation.error_body = str(exc)
            observation.usage = exc.usage
            observation.got_terminator = False
            state.finish(state.header_status)
            state.close_body()
            if body:
                yield body
            return
        observation.usage = decoded.usage
        observation.completion_chars = decoded.completion_chars
        if observation.completion_chars:
            state.clock.mark_content()
        observation.served_model = decoded.served_model
        observation.got_terminator = True
        state.finish(state.header_status)
        state.close_body()
        if decoded.client_body:
            yield decoded.client_body
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


def _stream_chunks(
    state: _RelayState, first: bytes, adapter: UpstreamAdapter
) -> Generator[bytes, None, None]:
    observation = state.observation
    assert state.body is not None
    tail: list[bytes] = []
    aborted = False
    translator = adapter.new_stream_translator(
        state.deps.model["upstream_model"], lambda _usage: None
    )
    try:
        try:
            for raw in _iter_events(_chunk_stream(state.body, first), state.clock):
                before_chars = translator.completion_chars
                translated: list[str] = []
                for line in raw.decode("utf-8", "replace").splitlines():
                    translated.extend(translator.feed_line(line))
                observation.usage = translator.usage
                observation.served_model = translator.served_model
                observation.completion_chars = translator.completion_chars
                if translator.completion_chars > before_chars:
                    state.clock.mark_content()
                client_bytes = raw if adapter.passthrough else "".join(translated).encode("utf-8")
                if tail or translator.terminal:
                    tail.append(client_bytes)
                    if translator.finished:
                        observation.got_terminator = True
                        break
                    continue
                yield client_bytes
        except _Cut as cut:
            observation.cut_by_proxy = cut.reason
            aborted = True
        except _Transport:
            observation.transport_failed = True
            aborted = True
        except UpstreamDecodeError as exc:
            observation.error_body = str(exc)
            observation.usage = exc.usage or translator.usage or observation.usage
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


def _iter_events(chunks: Iterator[bytes], clock: StreamClock) -> Iterator[bytes]:
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
            yield raw
    if buffer:
        yield buffer
