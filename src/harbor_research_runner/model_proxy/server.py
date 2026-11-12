from __future__ import annotations

import hashlib
import json
import logging
import os
import signal
import socket
import threading
import time
from collections.abc import Mapping
from contextlib import suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.metadata import version
from pathlib import Path
from typing import Any, cast

import httpx

from harbor_research_runner.model_proxy.adapters import UnsupportedAdapterPair, select_adapter
from harbor_research_runner.model_proxy.bodies import BodyStore
from harbor_research_runner.model_proxy.control import TrialRegistry
from harbor_research_runner.model_proxy.keys import KeyProvider, ModelCardView
from harbor_research_runner.model_proxy.ledger import LedgerUnavailable, LedgerWriter
from harbor_research_runner.model_proxy.pacing import Pacer
from harbor_research_runner.model_proxy.relay import RelayDeps, relay
from harbor_research_runner.records import jsonl
from harbor_research_runner.records.limits import Limits
from harbor_research_runner.records.proxy_control import (
    CLOSE_PATH,
    CLOSED_BODY,
    HEALTH_PATH,
    OPEN_PATH,
    CloseMessage,
    OpenMessage,
    ProxyConfig,
    trial_from_path,
)
from harbor_research_runner.records.proxy_ledger import ProxyStart

logger = logging.getLogger(__name__)

_UNAVAILABLE_BODY = b"proxy ledger unavailable"
_UNSUPPORTED_API_BODY = (
    b'{"error":{"message":"unsupported API for this model","type":"unsupported_api"}}'
)
_NOT_FOUND_BODY = b'{"error":{"message":"not found","type":"not_found"}}'
_INVALID_BODY = b'{"error":{"message":"invalid request","type":"invalid_request_error"}}'


class _ProxyState:
    def __init__(
        self,
        ledger: LedgerWriter,
        registry: TrialRegistry,
        relay_deps: RelayDeps,
        bodies: BodyStore,
    ) -> None:
        self.ledger = ledger
        self.registry = registry
        self.relay_deps = relay_deps
        self.bodies = bodies
        self.unavailable = threading.Event()
        self.server: ProxyHTTPServer | None = None

    def fail_ledger(self, detail: BaseException) -> None:
        if not self.unavailable.is_set():
            logger.error("proxy ledger unavailable: %s", detail)
            self.unavailable.set()
            if self.server is not None:
                self.server.stop_accepting()


class ProxyHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False

    def __init__(self, address: tuple[str, int], state: _ProxyState) -> None:
        self.state = state
        self.state.server = self
        self._handlers = threading.Condition()
        self._active_handlers = 0
        self._shutdown_started = threading.Event()
        super().__init__(address, _ProxyHandler)

    def process_request(self, request, client_address) -> None:
        with self._handlers:
            self._active_handlers += 1
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._handler_finished()
            raise

    def process_request_thread(self, request, client_address) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._handler_finished()

    def _handler_finished(self) -> None:
        with self._handlers:
            self._active_handlers -= 1
            self._handlers.notify_all()

    def wait_for_handlers(self, timeout_s: float) -> bool:
        deadline = time.monotonic() + timeout_s
        with self._handlers:
            while self._active_handlers:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._handlers.wait(remaining)
        return True

    def stop_accepting(self) -> None:
        if self._shutdown_started.is_set():
            return
        self._shutdown_started.set()
        threading.Thread(target=self.shutdown, daemon=True).start()

    def server_bind(self) -> None:
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.socket.bind(self.server_address)
        self.server_address = self.socket.getsockname()
        self.server_name, self.server_port = self.server_address[0], self.server_address[1]


def _watch_lifeline(fd: int, server: ProxyHTTPServer) -> None:
    try:
        while os.read(fd, 1):
            pass
    except OSError:
        pass
    finally:
        os.close(fd)
        server.stop_accepting()


class _ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    @property
    def state(self) -> _ProxyState:
        return cast(ProxyHTTPServer, self.server).state

    def do_GET(self) -> None:
        if self.state.unavailable.is_set():
            self._send(_UNAVAILABLE_BODY, 503)
            return
        if self.path != HEALTH_PATH:
            self._send(_NOT_FOUND_BODY, 404)
            return
        try:
            rows = self.state.ledger.history().rows
            seq = max((row.get("seq", 0) for row in rows), default=0)
        except LedgerUnavailable as exc:
            self.state.fail_ledger(exc)
            self._send(_UNAVAILABLE_BODY, 503)
            return
        body = json.dumps({"ok": True, "seq": seq}, separators=(",", ":")).encode()
        self._send(body, 200)

    def do_POST(self) -> None:
        if self.state.unavailable.is_set():
            self._send(_UNAVAILABLE_BODY, 503)
            return
        if self.path == OPEN_PATH:
            self._control(self._open)
            return
        if self.path == CLOSE_PATH:
            self._control(self._close)
            return
        route = trial_from_path(self.path)
        if route is None:
            self._send(_NOT_FOUND_BODY, 404)
            return
        trial, request_path = route
        if request_path not in {"/v1/chat/completions", "/v1/responses"}:
            self._send(_NOT_FOUND_BODY, 404)
            return
        try:
            adapter = select_adapter(self.state.relay_deps.model["api"], request_path)
        except UnsupportedAdapterPair:
            logger.warning(
                "unsupported adapter pair trial=%s path=%s api=%s",
                trial,
                request_path,
                self.state.relay_deps.model["api"],
            )
            self._send(_UNSUPPORTED_API_BODY, 400)
            return
        try:
            body = self._read_body()
        except ValueError:
            self._send(_INVALID_BODY, 400)
            return
        try:
            if not self.state.registry.admit(trial):
                self._send(CLOSED_BODY.encode(), 410)
                return
            response = relay(
                body,
                {key.lower(): value for key, value in self.headers.items()},
                trial,
                self.state.relay_deps,
                adapter,
            )
        except LedgerUnavailable as exc:
            self.state.fail_ledger(exc)
            self._send(_UNAVAILABLE_BODY, 503)
            return
        except (ValueError, json.JSONDecodeError):
            self._send(_INVALID_BODY, 400)
            return
        self._send_relay(response)

    def _control(self, handler) -> None:
        try:
            body = self._read_body()
        except ValueError:
            self._send(_INVALID_BODY, 400)
            return
        handler(body)

    def _open(self, body: bytes) -> None:
        try:
            message = OpenMessage.from_row(json.loads(body))
            status = self.state.registry.open(message)
        except (ValueError, TypeError, json.JSONDecodeError):
            self._send(_INVALID_BODY, 400)
            return
        except LedgerUnavailable as exc:
            self.state.fail_ledger(exc)
            self._send(_UNAVAILABLE_BODY, 503)
            return
        self._send(b"", status)

    def _close(self, body: bytes) -> None:
        try:
            message = CloseMessage.from_row(json.loads(body))
            status = self.state.registry.close(message)
        except (ValueError, TypeError, json.JSONDecodeError):
            self._send(_INVALID_BODY, 400)
            return
        except LedgerUnavailable as exc:
            self.state.fail_ledger(exc)
            self._send(_UNAVAILABLE_BODY, 503)
            return
        self.state.bodies.close_trial(message.trial)
        self._send(b"", status)

    def _send_relay(self, response) -> None:
        headers_sent = False
        try:
            self.send_response(response.http_status)
            for name, value in response.headers.items():
                self.send_header(name, value)
            self.send_header("Connection", "close")
            self.end_headers()
            headers_sent = True
            for chunk in response.chunks:
                self.wfile.write(chunk)
                self.wfile.flush()
        except LedgerUnavailable as exc:
            self.state.fail_ledger(exc)
            if not headers_sent:
                self._send(_UNAVAILABLE_BODY, 503)
        except (BrokenPipeError, ConnectionResetError, OSError):
            logger.info("proxy client disconnected")
        finally:
            try:
                response.close()
            except LedgerUnavailable as exc:
                self.state.fail_ledger(exc)
                if not headers_sent:
                    self._send(_UNAVAILABLE_BODY, 503)
            self.close_connection = True

    def _read_body(self) -> bytes:
        value = self.headers.get("Content-Length")
        if value is None:
            raise ValueError("missing content length")
        try:
            length = int(value)
        except ValueError as exc:
            raise ValueError("invalid content length") from exc
        if length < 0:
            raise ValueError("invalid content length")
        return self.rfile.read(length)

    def _send(self, body: bytes, status: int) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        if body:
            self.wfile.write(body)
            self.wfile.flush()
        self.close_connection = True

    def log_message(self, format: str, *args: Any) -> None:
        logger.info("proxy %s", format % args)


def serve(
    config: ProxyConfig,
    config_path: Path,
    environment: Mapping[str, str],
    lifeline_fd: int | None = None,
) -> int:
    ledger_path = Path(config.ledger_path)
    ledger = None
    try:
        old_rows = jsonl.read_rows(ledger_path).rows
        ledger = LedgerWriter(ledger_path)
        restart = bool(old_rows)
        ledger.write(
            ProxyStart(
                0,
                0.0,
                version("harbor-research-runner"),
                hashlib.sha256(config_path.read_bytes()).hexdigest(),
                restart,
            )
        )
        registry = TrialRegistry(ledger)
    except (OSError, LedgerUnavailable, ValueError) as exc:
        logger.error("proxy ledger unavailable at startup: %s", exc)
        if ledger is not None:
            with suppress(LedgerUnavailable):
                ledger.close()
        return 70

    model = config.model
    key_provider = KeyProvider(cast(ModelCardView, model), environment)
    limits = config.limits
    typed_limits = Limits.from_row(limits)
    tokens_per_minute = model.get("tokens_per_minute")
    pacer = None
    if tokens_per_minute is not None:
        cooldown_until = time.monotonic() + config.restart_cooldown_s if restart else None
        pacer = Pacer(tokens_per_minute, time.monotonic, time.sleep, cooldown_until)
    client = httpx.Client(trust_env=False)
    bodies_path = Path(config.bodies_dir) if config.bodies_dir is not None else None
    if bodies_path is not None:
        bodies_path.mkdir(parents=True, exist_ok=True)
    bodies = BodyStore(bodies_path)
    relay_deps = RelayDeps(
        ledger,
        model,
        typed_limits,
        client,
        key_provider.routes,
        time.monotonic,
        pacer,
        bodies,
    )
    state = _ProxyState(ledger, registry, relay_deps, bodies)
    server = ProxyHTTPServer((config.host, config.port), state)
    old_term = signal.signal(signal.SIGTERM, lambda signum, frame: server.stop_accepting())
    old_int = signal.signal(signal.SIGINT, lambda signum, frame: server.stop_accepting())
    if lifeline_fd is not None:
        threading.Thread(target=_watch_lifeline, args=(lifeline_fd, server), daemon=True).start()
    exit_code = 0
    try:
        server.serve_forever(poll_interval=0.05)
    finally:
        if not server.wait_for_handlers(10):
            logger.warning("proxy handlers did not drain within 10 seconds")
        server.server_close()
        client.close()
        try:
            ledger.close()
        except LedgerUnavailable as exc:
            logger.error("proxy ledger final sync failed: %s", exc)
            exit_code = 70
        if state.unavailable.is_set():
            exit_code = 70
        signal.signal(signal.SIGTERM, old_term)
        signal.signal(signal.SIGINT, old_int)
    return exit_code
