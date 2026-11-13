from __future__ import annotations

import json
import math
import re
import socket
import time
from contextlib import suppress
from dataclasses import dataclass, field
from functools import partial
from http.server import BaseHTTPRequestHandler
from threading import Event, Lock, Thread
from typing import Any

from harbor_research_runner.local_http import LocalHTTPServer


class _FakeUpstreamServer(LocalHTTPServer):
    pass


@dataclass
class RawResponse:
    chunks: list[bytes]
    hold_open: bool = False
    gate: Event | None = None
    gate_after: int = 0
    content_type: str = "text/event-stream"
    status_code: int = 200
    headers: dict[str, str] = field(default_factory=dict)
    chunk_delay_s: float = 0.0
    wait_for_client_close: bool = False
    ready: Event = field(default_factory=Event)
    release: Event = field(default_factory=Event)
    disconnected: Event = field(default_factory=Event)
    client_closed: Event = field(default_factory=Event)


class FakeUpstream:
    def __init__(self, script: list[str | RawResponse], model: str = "fake-model") -> None:
        if not script:
            raise ValueError("empty script")
        for entry in script:
            self._validate_entry(entry)
        self.script = script
        self.model = model
        self.requests: list[dict[str, Any]] = []
        self._server: _FakeUpstreamServer | None = None
        self._thread: Thread | None = None
        self._request_index = 0
        self._lock = Lock()

    def start(self) -> str:
        upstream = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                raw_body = self.rfile.read(length)
                try:
                    body = json.loads(raw_body or b"{}")
                except (json.JSONDecodeError, UnicodeDecodeError):
                    body = None
                script_entry = upstream._record_request(
                    {
                        "path": self.path,
                        "headers": {key.lower(): value for key, value in self.headers.items()},
                        "body": body,
                    }
                )
                upstream._respond(self, script_entry, body)

            def log_message(self, format: str, *args: object) -> None:
                return

        self._server = _FakeUpstreamServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._thread = Thread(
            target=partial(self._server.serve_forever, poll_interval=0.01), daemon=True
        )
        self._thread.start()
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    @staticmethod
    def _validate_entry(entry: str | RawResponse) -> None:
        if isinstance(entry, RawResponse):
            return
        if entry in {"ok", "rate_limit", "quota", "drop"}:
            return
        if re.fullmatch(r"ok:\d+", entry):
            return
        if re.fullmatch(r"http_\d{3}", entry) and 100 <= int(entry[5:]) <= 599:
            return
        if entry.startswith(("stall:", "gap:")):
            try:
                seconds = float(entry.split(":", 1)[1])
            except ValueError:
                seconds = -1
            if math.isfinite(seconds) and seconds >= 0:
                return
        if re.fullmatch(r"truncate_after:\d+", entry):
            return
        if entry.startswith("model:") and entry.removeprefix("model:"):
            return
        if entry.startswith("slow:"):
            try:
                rate = float(entry.removeprefix("slow:"))
            except ValueError:
                rate = 0
            if math.isfinite(rate) and rate > 0:
                return
        raise ValueError(f"unknown entry: {entry}")

    def stop(self) -> None:
        if self._server is None:
            return
        for entry in self.script:
            if isinstance(entry, RawResponse):
                entry.release.set()
                if entry.gate is not None:
                    entry.gate.set()
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join()
        self._server = None
        self._thread = None

    def _record_request(self, request: dict[str, Any]) -> str | RawResponse:
        with self._lock:
            self.requests.append(request)
            index = self._request_index
            self._request_index += 1
            return self.script[min(index, len(self.script) - 1)]

    def _respond(
        self, handler: BaseHTTPRequestHandler, entry: str | RawResponse, body: Any
    ) -> None:
        if handler.path not in ("/v1/chat/completions", "/v1/responses"):
            self._send_json(handler, 404, {"error": {"message": "not found"}})
            return
        if isinstance(entry, RawResponse):
            self._respond_raw(handler, entry)
            return
        if entry == "drop":
            handler.close_connection = True
            handler.connection.shutdown(2)
            handler.connection.close()
            return
        if entry.startswith("http_"):
            self._send_json(
                handler,
                int(entry.removeprefix("http_")),
                {"error": {"message": "scripted upstream error", "type": "server_error"}},
            )
            return
        if entry in ("rate_limit", "quota"):
            message = "quota is exhausted" if entry == "quota" else "rate limit reached"
            retry_after = "86400" if entry == "quota" else "1"
            self._send_json(
                handler,
                429,
                {"error": {"message": message, "type": entry}},
                {"Retry-After": retry_after},
            )
            return
        if entry.startswith("stall:"):
            time.sleep(float(entry.removeprefix("stall:")))
            handler.close_connection = True
            with suppress(OSError):
                handler.connection.shutdown(2)
            handler.connection.close()
            return
        if entry.startswith("truncate_after:"):
            count = int(entry.removeprefix("truncate_after:"))
            self._stream(handler, body, count=count, terminate=False)
            return
        if entry.startswith("gap:"):
            self._stream(handler, body, gap_s=float(entry.removeprefix("gap:")))
            return
        if entry.startswith("slow:"):
            pace = float(entry.removeprefix("slow:"))
            if body and body.get("stream"):
                self._stream(handler, body, pace_tokens_per_s=pace)
            else:
                time.sleep(20 / pace)
                self._send_json(handler, 200, self._complete_body(handler.path, 20))
            return
        if entry.startswith("model:"):
            model = entry.removeprefix("model:")
            if body and body.get("stream"):
                self._stream(handler, body, response_model=model)
            else:
                self._send_json(handler, 200, self._complete_body(handler.path, 20, model))
            return
        if entry == "ok" or entry.startswith("ok:"):
            completion_tokens = int(entry.split(":", 1)[1]) if ":" in entry else 20
            if body and body.get("stream"):
                self._stream(handler, body, completion_tokens=completion_tokens)
            else:
                self._send_json(handler, 200, self._complete_body(handler.path, completion_tokens))
            return
        raise ValueError(f"unknown fake upstream script entry: {entry}")

    @staticmethod
    def _respond_raw(handler: BaseHTTPRequestHandler, entry: RawResponse) -> None:
        handler.send_response(entry.status_code)
        handler.send_header("Content-Type", entry.content_type)
        handler.send_header("Cache-Control", "no-cache")
        handler.send_header("Connection", "close")
        for name, value in entry.headers.items():
            handler.send_header(name, value)
        handler.end_headers()
        entry.ready.set()
        for index, chunk in enumerate(entry.chunks):
            if entry.gate is not None and index == entry.gate_after:
                entry.gate.wait(timeout=5)
            if index and entry.chunk_delay_s:
                time.sleep(entry.chunk_delay_s)
            handler.wfile.write(chunk)
            handler.wfile.flush()
        if entry.hold_open:
            handler.connection.settimeout(0.02)
            while not entry.release.is_set():
                try:
                    if handler.connection.recv(1) == b"":
                        entry.disconnected.set()
                        break
                except TimeoutError:
                    continue
                except OSError:
                    entry.disconnected.set()
                    break
        if entry.wait_for_client_close:
            with suppress(OSError):
                handler.connection.shutdown(socket.SHUT_WR)
            handler.connection.settimeout(0.05)
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                try:
                    if handler.connection.recv(1) == b"":
                        entry.client_closed.set()
                        break
                except TimeoutError:
                    continue
                except OSError:
                    entry.client_closed.set()
                    break
        handler.close_connection = True

    def _stream(
        self,
        handler: BaseHTTPRequestHandler,
        request_body: Any,
        *,
        count: int = 5,
        terminate: bool = True,
        gap_s: float | None = None,
        pace_tokens_per_s: float | None = None,
        completion_tokens: int = 20,
        response_model: str | None = None,
    ) -> None:
        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream")
        handler.send_header("Cache-Control", "no-cache")
        handler.send_header("Connection", "close")
        handler.end_headers()
        model = response_model or self.model
        if handler.path == "/v1/responses":
            self._send_sse(
                handler,
                {
                    "type": "response.created",
                    "response": {
                        "id": "resp_fake",
                        "object": "response",
                        "model": model,
                        "status": "in_progress",
                    },
                },
                event="response.created",
            )
        contents = ("h", "e", "l", "l", "o")
        for index in range(count):
            content = contents[index % len(contents)]
            if gap_s is not None and index == 2:
                time.sleep(gap_s)
            if pace_tokens_per_s is not None:
                time.sleep(4 / pace_tokens_per_s)
            if handler.path == "/v1/chat/completions":
                chunk = {
                    "id": "chatcmpl-fake",
                    "object": "chat.completion.chunk",
                    "model": model,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"content": content},
                            "finish_reason": (
                                "stop" if index == count - 1 and count == 5 and terminate else None
                            ),
                        }
                    ],
                }
                self._send_sse(handler, chunk)
            else:
                self._send_sse(
                    handler,
                    {"type": "response.output_text.delta", "delta": content, "model": model},
                    event="response.output_text.delta",
                )
        if count == 5:
            if handler.path == "/v1/chat/completions":
                usage = {
                    "id": "chatcmpl-fake",
                    "object": "chat.completion.chunk",
                    "model": model,
                    "choices": [],
                    "usage": {
                        "prompt_tokens": 100,
                        "completion_tokens": completion_tokens,
                        "total_tokens": 100 + completion_tokens,
                        "prompt_tokens_details": {"cached_tokens": 0},
                    },
                }
                self._send_sse(handler, usage)
            else:
                completed = {
                    "type": "response.completed",
                    "response": {
                        "id": "resp_fake",
                        "object": "response",
                        "model": model,
                        "status": "completed",
                        "usage": {
                            "input_tokens": 100,
                            "output_tokens": completion_tokens,
                            "input_tokens_details": {"cached_tokens": 0},
                        },
                    },
                }
                self._send_sse(handler, completed, event="response.completed")
            if terminate and handler.path == "/v1/chat/completions":
                handler.wfile.write(b"data: [DONE]\n\n")
        handler.wfile.flush()
        handler.close_connection = True

    def _complete_body(
        self, path: str, completion_tokens: int, model: str | None = None
    ) -> dict[str, Any]:
        response_model = model or self.model
        if path == "/v1/chat/completions":
            return {
                "id": "chatcmpl-fake",
                "object": "chat.completion",
                "model": response_model,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "hello"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": completion_tokens,
                    "total_tokens": 100 + completion_tokens,
                    "prompt_tokens_details": {"cached_tokens": 0},
                },
            }
        return {
            "id": "resp_fake",
            "object": "response",
            "model": response_model,
            "status": "completed",
            "output": [{"type": "message", "content": [{"type": "output_text", "text": "hello"}]}],
            "usage": {
                "input_tokens": 100,
                "output_tokens": completion_tokens,
                "input_tokens_details": {"cached_tokens": 0},
            },
        }

    def _send_json(
        self,
        handler: BaseHTTPRequestHandler,
        status: int,
        value: dict[str, Any],
        headers: dict[str, str] | None = None,
    ) -> None:
        payload = json.dumps(value, separators=(",", ":")).encode()
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(payload)))
        for name, header_value in (headers or {}).items():
            handler.send_header(name, header_value)
        handler.end_headers()
        handler.wfile.write(payload)

    def _send_sse(
        self,
        handler: BaseHTTPRequestHandler,
        value: dict[str, Any],
        *,
        event: str | None = None,
    ) -> None:
        if event is not None:
            handler.wfile.write(f"event: {event}\n".encode())
        handler.wfile.write(f"data: {json.dumps(value, separators=(',', ':'))}\n\n".encode())
        handler.wfile.flush()
