from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from harbor_research_runner.run import process_group

needs_group = pytest.mark.skipif(not hasattr(os, "killpg"), reason="process groups need killpg")


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_pid(path: Path, timeout: float = 2.0) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            pid = int(path.read_text(encoding="utf-8").strip())
            if pid > 0:
                return pid
        except (OSError, ValueError):
            pass
        time.sleep(0.05)
    raise AssertionError(f"{path} never held a positive PID")


def _wait_dead(pid: int, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            return True
        time.sleep(0.05)
    return not _pid_alive(pid)


def _wait_path(path: Path, timeout: float = 2.0) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            return path.read_text(encoding="utf-8").strip()
        except OSError:
            time.sleep(0.05)
    raise AssertionError(f"{path} never appeared")


def _stop_and_cleanup(process: subprocess.Popen, **kwargs: float) -> int:
    try:
        return process_group.stop_group(process, **kwargs)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=2.0)


def _grandchild_command(pidfile: Path) -> list[str]:
    code = "\n".join(
        [
            "import subprocess, sys, time",
            "grandchild = subprocess.Popen(['sleep', '60'])",
            "open(sys.argv[1], 'w').write(str(grandchild.pid))",
            "time.sleep(60)",
        ]
    )
    return [sys.executable, "-c", code, str(pidfile)]


def _sh_grandchild_command(pidfile: Path) -> list[str]:
    return ["sh", "-c", f'sleep 60 & echo $! > "{pidfile}"; wait']


def _member_group_command(member_code: str, pidfile: Path, marker: Path) -> list[str]:
    return [
        "sh",
        "-c",
        '"$@" & exec sleep 1',
        "sh",
        sys.executable,
        "-c",
        member_code,
        str(pidfile),
        str(marker),
    ]


@needs_group
def test_spawn_group_starts_a_new_session() -> None:
    process = process_group.spawn_group(
        [sys.executable, "-c", "import os; print(os.getpgid(0))"],
        env={},
        stdout=subprocess.PIPE,
    )
    try:
        assert process.stdout is not None
        pgid = int(process.stdout.read().decode().strip())
        assert process.wait(timeout=2.0) == 0
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=2.0)
    assert pgid == process.pid
    assert pgid != os.getpgrp()


@needs_group
def test_stop_group_tolerates_vanished_group() -> None:
    process = process_group.spawn_group([sys.executable, "-c", "pass"], env={})
    assert process.wait(timeout=2.0) == 0

    assert process_group.stop_group(process, grace_s=0.2) == 0


@needs_group
def test_stop_group_stops_a_child_and_its_grandchild(tmp_path: Path) -> None:
    pidfile = tmp_path / "grandchild.pid"
    process = process_group.spawn_group(_grandchild_command(pidfile), env=dict(os.environ))
    grandchild = _wait_pid(pidfile)
    assert _pid_alive(grandchild)

    _stop_and_cleanup(process, grace_s=0.5, escalation_grace_s=0.5)

    assert process.poll() is not None
    assert _wait_dead(grandchild)


@needs_group
def test_stop_group_kills_a_child_that_ignores_sigterm_after_the_grace(tmp_path: Path) -> None:
    readyfile = tmp_path / "ready"
    process = process_group.spawn_group(
        ["sh", "-c", f'trap "" INT TERM; touch "{readyfile}"; while true; do sleep 1; done'],
        env={},
    )
    _wait_path(readyfile)
    started = time.monotonic()

    rc = _stop_and_cleanup(process, grace_s=0.2, escalation_grace_s=0.2)

    elapsed = time.monotonic() - started
    assert rc == -signal.SIGKILL
    assert process.poll() is not None
    assert elapsed >= 0.3


@needs_group
@pytest.mark.parametrize("shape", ["python", "sh"])
def test_stop_group_kill_only_clears_grandchildren(tmp_path: Path, shape: str) -> None:
    pidfile = tmp_path / f"grandchild-{shape}.pid"
    command = _grandchild_command(pidfile) if shape == "python" else _sh_grandchild_command(pidfile)
    env = dict(os.environ) if shape == "python" else {}
    process = process_group.spawn_group(command, env=env)
    grandchild = _wait_pid(pidfile)
    assert _pid_alive(grandchild)

    _stop_and_cleanup(process, grace_s=0.1, kill_only=True)

    assert process.poll() is not None
    assert _wait_dead(grandchild)


@needs_group
def test_stop_group_lets_members_finish_within_step_grace(tmp_path: Path) -> None:
    pidfile = tmp_path / "member-slow.pid"
    marker = tmp_path / "member-slow.done"
    member_code = "\n".join(
        [
            "import os, signal, sys, time",
            "signal.signal(signal.SIGINT, signal.SIG_IGN)",
            "open(sys.argv[1], 'w').write(str(os.getpid()))",
            "def _on_term(n, f):",
            "    time.sleep(0.05)",
            "    open(sys.argv[2], 'w').write('done')",
            "    sys.exit(0)",
            "signal.signal(signal.SIGTERM, _on_term)",
            "time.sleep(1)",
        ]
    )
    process = process_group.spawn_group(_member_group_command(member_code, pidfile, marker), env={})
    member_pid = _wait_pid(pidfile)

    rc = _stop_and_cleanup(process, grace_s=0.2, escalation_grace_s=1.0)

    assert marker.read_text(encoding="utf-8") == "done"
    assert _wait_dead(member_pid)
    assert rc == -signal.SIGINT


@needs_group
def test_stop_group_bounds_a_member_that_ignores_both_signals(tmp_path: Path) -> None:
    pidfile = tmp_path / "member-stubborn.pid"
    marker = tmp_path / "member-stubborn.done"
    member_code = "\n".join(
        [
            "import os, signal, sys, time",
            "signal.signal(signal.SIGINT, signal.SIG_IGN)",
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)",
            "open(sys.argv[1], 'w').write(str(os.getpid()))",
            "time.sleep(1)",
        ]
    )
    process = process_group.spawn_group(_member_group_command(member_code, pidfile, marker), env={})
    member_pid = _wait_pid(pidfile)
    started = time.monotonic()

    rc = _stop_and_cleanup(process, grace_s=0.2, escalation_grace_s=0.2)

    elapsed = time.monotonic() - started
    assert _wait_dead(member_pid)
    assert 0.3 <= elapsed < 1.0
    assert rc == -signal.SIGINT
