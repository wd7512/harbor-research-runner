from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
import time
from contextlib import suppress
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


def test_continue_on_error_attempts_remaining_requests() -> None:
    upstream = FakeUpstream(["http_500", "ok", "ok"])
    base_url = upstream.start()
    try:
        result = _run(
            "--base-url",
            f"{base_url}/v1",
            "--requests",
            "3",
            "--continue-on-error",
        )
    finally:
        upstream.stop()

    assert result.returncode == 0
    assert len(upstream.requests) == 3


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


def test_detached_heartbeat_survives_harness_group_stop(tmp_path: Path) -> None:
    heartbeat = tmp_path / "detached-heartbeat.txt"
    process = subprocess.Popen(
        [
            sys.executable,
            str(_SCRIPT),
            "--base-url",
            "http://127.0.0.1:1/v1",
            "--requests",
            "0",
            "--sleep-s",
            "30",
            "--detached-heartbeat",
            str(heartbeat),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    heartbeat_pid: int | None = None
    try:
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and not heartbeat.exists():
            time.sleep(0.05)
        assert heartbeat.exists()
        first_contents = heartbeat.read_text(encoding="utf-8")
        assert all(float(line) > 0 for line in first_contents.splitlines())
        heartbeat_pid = _heartbeat_process_id(heartbeat)
        assert os.getpgid(heartbeat_pid) != process.pid

        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=2)

        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if heartbeat.read_text(encoding="utf-8") != first_contents:
                break
            time.sleep(0.05)
        assert heartbeat.read_text(encoding="utf-8") != first_contents
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=2)
        if heartbeat_pid is None:
            with suppress(AssertionError, subprocess.CalledProcessError, ValueError):
                heartbeat_pid = _heartbeat_process_id(heartbeat)
        if heartbeat_pid is not None:
            with suppress(ProcessLookupError):
                os.kill(heartbeat_pid, signal.SIGTERM)


def test_detached_requester_survives_harness_group_stop() -> None:
    upstream = FakeUpstream(["ok"])
    base_url = upstream.start()
    process: subprocess.Popen[str] | None = None
    started = time.monotonic()
    try:
        process = subprocess.Popen(
            [
                sys.executable,
                str(_SCRIPT),
                "--base-url",
                f"{base_url}/v1",
                "--requests",
                "0",
                "--sleep-s",
                "30",
                "--detached-requesting-s",
                "1.5",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and not upstream.requests:
            time.sleep(0.05)
        assert upstream.requests
        requests_before_stop = len(upstream.requests)

        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=2)

        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and len(upstream.requests) <= requests_before_stop:
            time.sleep(0.05)
        assert len(upstream.requests) > requests_before_stop
        while time.monotonic() < started + 2:
            time.sleep(0.05)
    finally:
        if process is not None and process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=2)
        upstream.stop()


def _heartbeat_process_id(heartbeat: Path) -> int:
    result = subprocess.run(
        ["ps", "-axo", "pid=,pgid=,command="],
        capture_output=True,
        text=True,
        check=True,
    )
    matching = [
        line
        for line in result.stdout.splitlines()
        if str(heartbeat) in line and re.search(r"(^|\s)-c(\s|$)", line)
    ]
    assert len(matching) == 1, result.stdout
    return int(matching[0].split(None, 1)[0])
