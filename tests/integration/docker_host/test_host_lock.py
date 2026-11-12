import multiprocessing
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from harbor_research_runner.docker_host.host_lock import (
    HostLock,
    HostLockUnavailable,
)


def _race_for_lock(path, start, release, results):
    start.wait()
    lock = HostLock(Path(path))
    try:
        lock.acquire()
    except HostLockUnavailable:
        results.put("unavailable")
        return
    results.put("acquired")
    release.wait()
    lock.release()


def _hold_lock(path, ready):
    lock = HostLock(Path(path))
    lock.acquire()
    ready.set()
    while True:
        time.sleep(1)


def _hold_lock_with_grandchild(path, ready_file, grandchild_file):
    lock = HostLock(Path(path))
    lock.acquire()
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    Path(grandchild_file).write_text(str(child.pid))
    Path(ready_file).touch()
    while True:
        time.sleep(1)


def test_two_processes_yield_one_host_lease(tmp_path):
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    release = context.Event()
    results = context.Queue()
    path = tmp_path / "host.lock"
    children = [
        context.Process(target=_race_for_lock, args=(str(path), start, release, results))
        for _ in range(2)
    ]
    for child in children:
        child.start()
    start.set()
    try:
        outcomes = [results.get(timeout=3) for _ in children]
    finally:
        release.set()
        for child in children:
            child.join(timeout=3)

    assert sorted(outcomes) == ["acquired", "unavailable"]
    assert all(child.exitcode == 0 for child in children)


def test_host_lock_is_released_when_holder_is_killed(tmp_path):
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    path = tmp_path / "host.lock"
    child = context.Process(target=_hold_lock, args=(str(path), ready))
    child.start()
    try:
        assert ready.wait(timeout=3)
        os.kill(child.pid, signal.SIGKILL)
        child.join(timeout=3)

        lock = HostLock(path)
        lock.acquire()
        lock.release()
        assert child.exitcode == -signal.SIGKILL
    finally:
        if child.is_alive():
            os.kill(child.pid, signal.SIGKILL)
            child.join(timeout=3)


def test_lock_is_not_held_by_a_child_after_the_parent_dies(tmp_path):
    context = multiprocessing.get_context("spawn")
    path = tmp_path / "host.lock"
    ready_file = tmp_path / "ready"
    grandchild_file = tmp_path / "grandchild.pid"
    parent = context.Process(
        target=_hold_lock_with_grandchild,
        args=(str(path), str(ready_file), str(grandchild_file)),
    )
    parent.start()
    deadline = time.monotonic() + 3
    while not ready_file.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    grandchild_pid = None
    try:
        assert ready_file.exists()
        grandchild_pid = int(grandchild_file.read_text())

        os.kill(parent.pid, signal.SIGKILL)
        parent.join(timeout=3)

        lock = HostLock(path)
        lock.acquire()
        lock.release()
    finally:
        if parent.is_alive():
            os.kill(parent.pid, signal.SIGKILL)
            parent.join(timeout=3)
        if grandchild_pid is not None:
            os.kill(grandchild_pid, signal.SIGTERM)
