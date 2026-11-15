from __future__ import annotations

import os
import time

import pytest

from harbor_research_runner.run import process_group

needs_group = pytest.mark.skipif(not hasattr(os, "killpg"), reason="process groups need killpg")


class _OwnGroupProcess:
    pid = os.getpgrp()


class _ExitingProcess:
    pid = 42424

    def poll(self) -> None:
        return None

    def wait(self, timeout: float | None = None) -> int:
        return 0

    def send_signal(self, sig: int) -> None:
        raise AssertionError("the direct child must not be signalled")

    def kill(self) -> None:
        raise AssertionError("the stop must not escalate to a direct SIGKILL")


@needs_group
def test_stop_group_refuses_our_own_process_group() -> None:
    with pytest.raises(RuntimeError, match="own process group"):
        process_group.stop_group(_OwnGroupProcess(), grace_s=0.1)


@needs_group
def test_stop_group_tolerates_a_transient_permission_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = {"n": 0}

    def killpg(pgid: int, sig: int) -> None:
        if sig == 0:
            raise ProcessLookupError
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(os, "killpg", killpg)
    monkeypatch.setattr(time, "sleep", lambda seconds: None)

    assert process_group.stop_group(_ExitingProcess(), grace_s=0.1) == 0


@needs_group
def test_stop_group_raises_when_a_permission_error_persists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def killpg(pgid: int, sig: int) -> None:
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(os, "killpg", killpg)
    monkeypatch.setattr(time, "sleep", lambda seconds: None)

    with pytest.raises(PermissionError):
        process_group.stop_group(_ExitingProcess(), grace_s=0.1)
