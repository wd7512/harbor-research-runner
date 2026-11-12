import socket

import pytest

from harbor_research_runner.run.checks import _bind_port


def _closed_connection_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        client = socket.create_connection(("127.0.0.1", port))
        accepted, _ = listener.accept()
        accepted.close()
        client.close()
    return port


def test_port_left_in_time_wait_by_a_stopped_proxy_is_free() -> None:
    _bind_port("127.0.0.1", _closed_connection_port())


def test_port_with_a_live_listener_is_in_use() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        with pytest.raises(OSError):
            _bind_port("127.0.0.1", listener.getsockname()[1])
