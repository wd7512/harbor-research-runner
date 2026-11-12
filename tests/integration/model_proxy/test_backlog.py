from __future__ import annotations

import socket
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

from harbor_research_runner.model_proxy.server import ProxyHTTPServer


def test_proxy_accepts_64_connections_while_accept_loop_is_busy() -> None:
    server = ProxyHTTPServer(("127.0.0.1", 0), SimpleNamespace(server=None))
    address = server.server_address

    def connect(_: int) -> socket.socket:
        connection = socket.create_connection(address, timeout=0.5)
        return connection

    connections = []
    try:
        with ThreadPoolExecutor(max_workers=64) as executor:
            connections = list(executor.map(connect, range(64)))
    finally:
        for connection in connections:
            connection.close()
        server.server_close()
