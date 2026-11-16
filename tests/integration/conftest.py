from __future__ import annotations

import resource
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import pytest

from harbor_research_runner.records.limits import Limits
from harbor_research_runner.records.proxy_control import ProxyConfig, write
from tests.support.fake_upstream import FakeUpstream


@dataclass
class ProxyProcess:
    url: str
    ledger_path: Path
    config_path: Path
    upstream: FakeUpstream
    file_size_limit: int | None
    process: subprocess.Popen | None = None

    def start(self) -> subprocess.Popen:
        preexec_fn = None
        if self.file_size_limit is not None:
            limit = self.file_size_limit

            def set_file_size_limit() -> None:
                signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
                resource.setrlimit(resource.RLIMIT_FSIZE, (limit, limit))

            preexec_fn = set_file_size_limit
        self.process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "harbor_research_runner.model_proxy",
                "--config",
                str(self.config_path),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            preexec_fn=preexec_fn,
        )
        deadline = time.monotonic() + 10
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            assert self.process is not None
            code = self.process.poll()
            if code is not None:
                output = self.process.communicate()[0]
                raise AssertionError(f"proxy exited during startup with {code}: {output}")
            try:
                with urllib.request.urlopen(f"{self.url}/control/health", timeout=0.1) as response:
                    if response.status == 200:
                        return self.process
            except (OSError, urllib.error.URLError) as exc:
                last_error = exc
            time.sleep(0.01)
        self.stop()
        raise AssertionError(f"proxy did not become healthy: {last_error}")

    def stop(self) -> int | None:
        if self.process is None:
            return None
        if self.process.poll() is None:
            self.process.send_signal(signal.SIGTERM)
        try:
            return self.process.wait(timeout=12)
        except subprocess.TimeoutExpired:
            self.process.kill()
            return self.process.wait(timeout=2)

    def restart(self) -> subprocess.Popen:
        assert self.process is not None
        if self.process.poll() is None:
            raise AssertionError("proxy must be stopped before restart")
        return self.start()


@pytest.fixture
def proxy(tmp_path: Path, request: pytest.FixtureRequest):
    options = getattr(request, "param", {})
    upstream = FakeUpstream(options.get("script", ["ok"]))
    upstream_url = upstream.start()
    with socket.socket() as reserve:
        reserve.bind(("127.0.0.1", 0))
        port = reserve.getsockname()[1]
    ledger_path = tmp_path / "proxy-ledger.jsonl"
    config_path = tmp_path / "proxy-config.json"
    limits = Limits.from_dict({}, repetitions=1, verifier_timeout_multiplier=1.0)
    write(
        config_path,
        ProxyConfig(
            "127.0.0.1",
            port,
            str(ledger_path),
            str(tmp_path / "request-bodies"),
            {
                "id": "fake/model",
                "upstream_model": "fake-model",
                "api": "chat",
                "endpoints": [f"{upstream_url}/v1/chat/completions"],
                "key_env": [],
                "tokens_per_minute": None,
            },
            limits.to_row(),
            0.0,
        ),
    )
    running = ProxyProcess(
        f"http://127.0.0.1:{port}",
        ledger_path,
        config_path,
        upstream,
        options.get("file_size_limit"),
    )
    try:
        running.start()
        yield running
    finally:
        running.stop()
        upstream.stop()
