"""Process-group spawn and stop for runner children.

Every runner child (`uv run ...`) is a launcher whose real work happens in
a grandchild, so killing only the direct child orphans the spender. These
helpers put each child in its own session and reach the whole group on
every kill path.

Safety is a precondition, not a fallback: `_signal_group` raises instead
of signaling when the target pgid is the runner's own group, so a child
that never left the group can only ever get direct-child signals.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import time
from contextlib import suppress

logger = logging.getLogger(__name__)

_VERIFY_LOOP_S = 2.0
_VERIFY_POLL_S = 0.05
_STEP_POLL_S = 0.05
_EPERM_RETRIES = 10


def spawn_group(command: list[str], *, env: dict[str, str], **popen_kwargs) -> subprocess.Popen:
    """Start `command` in its own session when the platform supports one."""
    if hasattr(os, "setsid"):
        popen_kwargs["start_new_session"] = True
    return subprocess.Popen(command, env=env, **popen_kwargs)


def _signal_group(pgid: int, sig: int) -> None:
    if pgid == os.getpgrp():
        raise RuntimeError(f"refusing to signal our own process group {pgid}")
    os.killpg(pgid, sig)


def _signal_group_past_exit_race(process: subprocess.Popen, sig: int) -> None:
    for _ in range(_EPERM_RETRIES):
        try:
            _signal_group(process.pid, sig)
            return
        except PermissionError:
            if process.poll() is not None:
                raise
            time.sleep(_VERIFY_POLL_S)
    _signal_group(process.pid, sig)


def _send(process: subprocess.Popen, sig: int) -> bool:
    try:
        _signal_group_past_exit_race(process, sig)
    except PermissionError:
        if process.poll() is not None:
            logger.debug(
                "pid %s already reaped; skipping the missed group %s",
                process.pid,
                signal.Signals(sig).name,
            )
            return False
        raise
    except ProcessLookupError:
        if process.poll() is not None:
            logger.debug(
                "pid %s already exited; skipping the missed group %s",
                process.pid,
                signal.Signals(sig).name,
            )
            return False
        logger.info(
            "process group of pid %s is gone while it lives on "
            "(it left its process group); signaling the process itself",
            process.pid,
        )
        try:
            process.send_signal(sig)
        except ProcessLookupError:
            logger.debug(
                "pid %s exited before the direct %s",
                process.pid,
                signal.Signals(sig).name,
            )
            return False
    return True


def _wait(process: subprocess.Popen, timeout: float) -> int | None:
    try:
        return process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        return None


def _wait_step(process: subprocess.Popen, pgid: int, timeout: float) -> int | None:
    """Wait for the whole group to be empty or `timeout` to run out.

    Signals may leave the leader long before its group members finish
    their own teardown, so a step ends on the group, not the leader:
    either everything is gone or the step's grace is spent. Returns the
    leader's exit code, or None while the group is still occupied.
    """
    deadline = time.monotonic() + timeout
    while True:
        rc = _wait(process, _STEP_POLL_S)
        if _group_empty(pgid):
            return rc
        if time.monotonic() >= deadline:
            return None


def _verify_empty(pgid: int) -> None:
    deadline = time.monotonic() + _VERIFY_LOOP_S
    while True:
        try:
            _signal_group(pgid, 0)
        except (ProcessLookupError, PermissionError):
            return
        if time.monotonic() >= deadline:
            logger.error(
                "process group %s still has members; orphaned trials may outlive this teardown",
                pgid,
            )
            return
        time.sleep(_VERIFY_POLL_S)


def _group_empty(pgid: int) -> bool:
    try:
        _signal_group(pgid, 0)
    except (ProcessLookupError, PermissionError):
        return True
    return False


def _reap(process: subprocess.Popen, pgid: int, timeout: float) -> int:
    rc = _wait(process, timeout)
    if rc is None:
        logger.error(
            "pid %s is still alive after the group SIGKILL; "
            "orphaned trials may outlive this teardown",
            process.pid,
        )
        rc = -signal.SIGKILL
    _verify_empty(pgid)
    return rc


def _stop_direct_child(
    process: subprocess.Popen, grace_s: float, escalation_grace_s: float, kill_only: bool
) -> int:
    if kill_only:
        with suppress(OSError):
            process.kill()
        rc = _wait(process, grace_s)
        return -signal.SIGKILL if rc is None else rc
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            process.send_signal(sig)
        except ProcessLookupError:
            if process.poll() is None:
                raise
        rc = _wait(process, grace_s if sig == signal.SIGINT else escalation_grace_s)
        if rc is not None:
            return rc
    with suppress(OSError):
        process.kill()
    rc = _wait(process, escalation_grace_s)
    return -signal.SIGKILL if rc is None else rc


def stop_group(
    process: subprocess.Popen,
    *,
    grace_s: float,
    escalation_grace_s: float | None = None,
    kill_only: bool = False,
) -> int:
    """Stop `process` and every member of its process group.

    With a group, SIGINT goes to the group first so the child runs its own
    teardown, then SIGTERM, then SIGKILL. Each step waits for the whole
    group — leader and members alike — to be empty, or for that step's
    grace to run out, so members that outlive the leader still get their
    grace before the next signal; the direct child is reaped and the group
    is verified empty before returning, and the return value stays the
    leader's exit code. `kill_only` keeps the sites that today go straight
    to SIGKILL on SIGKILL, sent to the group. Without a group, the current
    direct-child behaviour runs.
    """
    escalation = escalation_grace_s if escalation_grace_s is not None else grace_s
    if not hasattr(os, "killpg"):
        return _stop_direct_child(process, grace_s, escalation, kill_only)
    if process.pid == os.getpgrp():
        raise RuntimeError(f"refusing to stop our own process group {process.pid}")
    pgid = process.pid
    if kill_only:
        _send(process, signal.SIGKILL)
        return _reap(process, pgid, grace_s)
    _send(process, signal.SIGINT)
    rc = _wait_step(process, pgid, grace_s)
    if rc is not None:
        return rc
    _send(process, signal.SIGTERM)
    rc = _wait_step(process, pgid, escalation)
    if rc is not None:
        return rc
    _send(process, signal.SIGKILL)
    return _reap(process, pgid, escalation)
