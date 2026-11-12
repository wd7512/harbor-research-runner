from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from tests.support.fake_upstream import FakeUpstream

_SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "harbor_research_runner"
    / "harness"
    / "fake_script.py"
)


def _run(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(_SCRIPT), *arguments],
        capture_output=True,
        text=True,
        timeout=4,
        check=False,
    )


def test_three_requests_reach_the_fake_upstream() -> None:
    upstream = FakeUpstream(["ok"])
    base_url = upstream.start()
    try:
        result = _run("--base-url", f"{base_url}/v1", "--requests", "3")
    finally:
        upstream.stop()

    assert result.returncode == 0
    assert len(upstream.requests) == 3
    assert all(request["path"] == "/v1/chat/completions" for request in upstream.requests)
    assert all(request["body"]["stream"] is True for request in upstream.requests)


def test_solve_writes_hello_to_the_overridden_output(tmp_path: Path) -> None:
    output = tmp_path / "hello.txt"

    result = _run(
        "--base-url",
        "http://127.0.0.1:1/v1",
        "--requests",
        "0",
        "--solve",
        "--out",
        str(output),
    )

    assert result.returncode == 0
    assert output.read_bytes() == b"hello\n"


def test_a_request_failure_exits_nonzero() -> None:
    upstream = FakeUpstream(["http_500"])
    base_url = upstream.start()
    try:
        result = _run("--base-url", f"{base_url}/v1", "--requests", "1")
    finally:
        upstream.stop()

    assert result.returncode != 0


def test_keep_requesting_continues_through_refusals() -> None:
    upstream = FakeUpstream(["http_500"])
    base_url = upstream.start()
    try:
        result = _run(
            "--base-url",
            f"{base_url}/v1",
            "--requests",
            "0",
            "--keep-requesting-s",
            "0.1",
            "--exit-code",
            "0",
        )
    finally:
        upstream.stop()

    assert result.returncode == 0
    assert len(upstream.requests) >= 1
