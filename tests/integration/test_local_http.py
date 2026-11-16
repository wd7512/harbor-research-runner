from __future__ import annotations

import socket
from http.server import BaseHTTPRequestHandler
from types import SimpleNamespace

import pytest

from harbor_research_runner.local_http import LocalHTTPServer
from harbor_research_runner.model_proxy.server import ProxyHTTPServer
from harbor_research_runner.results_explorer.loader import EvidenceStore
from harbor_research_runner.results_explorer.server import build_server


@pytest.mark.parametrize(
    "server_factory",
    [
        lambda: LocalHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler),
        lambda: ProxyHTTPServer(("127.0.0.1", 0), SimpleNamespace(server=None)),
        lambda: build_server(EvidenceStore([]), port=0),
    ],
)
def test_loopback_servers_bind_without_reverse_lookup(monkeypatch, server_factory) -> None:
    def reject_reverse_lookup(host: str) -> str:
        raise AssertionError("reverse lookup")

    monkeypatch.setattr(socket, "getfqdn", reject_reverse_lookup)
    server = server_factory()
    try:
        assert server.server_name == "127.0.0.1"
        assert server.server_port == server.server_address[1]
        assert server.request_queue_size == 128
    finally:
        server.server_close()
