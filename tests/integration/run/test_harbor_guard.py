from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest


def _guard_start(
    tmp_path: Path,
    child: list[str],
    *,
    grace_s: float = 0.2,
    via_cli: bool = False,
) -> tuple[subprocess.Popen, int]:
    read_fd, write_fd = os.pipe()
    if via_cli:
        command = [
            sys.executable,
            "-m",
            "harbor_research_runner.run.harbor_guard",
            "--lifeline-fd",
            str(read_fd),
            "--",
            *child,
        ]
    else:
        code = (
            "import sys; "
            "from harbor_research_runner.run.harbor_guard import run_guard; "
            "raise SystemExit(run_guard(sys.argv[3:], int(sys.argv[2]), grace_s=float(sys.argv[1])))"
        )
        command = [sys.executable, "-c", code, str(grace_s), str(read_fd), *child]
    process = subprocess.Popen(
        command,
        pass_fds=(read_fd,),
        start_new_session=True,
    )
    os.close(read_fd)
    return process, write_fd


def _heartbeat_child(tmp_path: Path, *, ignore_term: bool) -> tuple[list[str], Path, Path]:
    heartbeat = tmp_path / "heartbeat"
    terminated = tmp_path / "terminated"
    code = "\n".join(
        [
            "import pathlib, signal, sys, time",
            "heartbeat = pathlib.Path(sys.argv[1])",
            "terminated = pathlib.Path(sys.argv[2])",
            "def stop(*_):",
            "    terminated.write_text('term', encoding='utf-8')",
            "    raise SystemExit(0)",
            f"signal.signal(signal.SIGTERM, {'signal.SIG_IGN' if ignore_term else 'stop'})",
            "counter = 0",
            "while True:",
            "    heartbeat.write_text(str(counter), encoding='utf-8')",
            "    counter += 1",
            "    time.sleep(0.02)",
        ]
    )
    return [sys.executable, "-c", code, str(heartbeat), str(terminated)], heartbeat, terminated


def _wait_for_file(path: Path, timeout: float = 2.0) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            time.sleep(0.01)
    raise AssertionError(f"{path} was not written")


@pytest.mark.timeout(5)
def test_harbor_guard_signals_child_when_lifeline_closes(tmp_path: Path) -> None:
    child, _heartbeat, terminated = _heartbeat_child(tmp_path, ignore_term=False)
    process, write_fd = _guard_start(tmp_path, child)
    try:
        _wait_for_file(_heartbeat)
        os.close(write_fd)
        write_fd = -1
        assert process.wait(timeout=2.0) == 0
        assert _wait_for_file(terminated) == "term"
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=2.0)
        if write_fd >= 0:
            os.close(write_fd)


@pytest.mark.timeout(5)
def test_harbor_guard_kills_group_after_lifeline_grace(tmp_path: Path) -> None:
    child, heartbeat, terminated = _heartbeat_child(tmp_path, ignore_term=True)
    process, write_fd = _guard_start(tmp_path, child, grace_s=0.1)
    try:
        first_heartbeat = _wait_for_file(heartbeat)
        os.close(write_fd)
        write_fd = -1
        assert process.wait(timeout=2.0) == -signal.SIGKILL
        time.sleep(0.05)
        stopped_heartbeat = heartbeat.read_text(encoding="utf-8")
        time.sleep(0.05)
        assert heartbeat.read_text(encoding="utf-8") == stopped_heartbeat
        assert stopped_heartbeat != first_heartbeat
        assert not terminated.exists()
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=2.0)
        if write_fd >= 0:
            os.close(write_fd)


@pytest.mark.timeout(5)
def test_harbor_guard_returns_harbor_exit_code_while_lifeline_is_open(
    tmp_path: Path,
) -> None:
    process, write_fd = _guard_start(
        tmp_path,
        [sys.executable, "-c", "raise SystemExit(23)"],
        via_cli=True,
    )
    try:
        assert process.wait(timeout=2.0) == 23
    finally:
        os.close(write_fd)


@pytest.mark.timeout(5)
def test_group_sigterm_reaches_harbor_child(tmp_path: Path) -> None:
    child, _heartbeat, terminated = _heartbeat_child(tmp_path, ignore_term=False)
    process, write_fd = _guard_start(tmp_path, child)
    try:
        _wait_for_file(_heartbeat)
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=2.0)
        assert _wait_for_file(terminated) == "term"
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=2.0)
        os.close(write_fd)
