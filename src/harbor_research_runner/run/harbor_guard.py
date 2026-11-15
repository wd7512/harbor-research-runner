from __future__ import annotations

import argparse
import os
import selectors
import signal
import subprocess
import sys
from collections.abc import Sequence

from harbor_research_runner.run.process_group import HARBOR_STOP_GRACE_S


def run_guard(command: list[str], lifeline_fd: int, *, grace_s: float = HARBOR_STOP_GRACE_S) -> int:
    if not command:
        raise ValueError("Harbor command is empty")
    if os.getsid(0) != os.getpgrp():
        raise RuntimeError("Harbor guard must run in its own process group")
    with selectors.DefaultSelector() as selector:
        selector.register(lifeline_fd, selectors.EVENT_READ)
        harbor = subprocess.Popen(command)
        try:
            while harbor.poll() is None:
                try:
                    ready = selector.select(timeout=0.1)
                except InterruptedError:
                    continue
                if not ready:
                    continue
                try:
                    value = os.read(lifeline_fd, 1)
                except InterruptedError:
                    continue
                if value:
                    continue
                try:
                    harbor.send_signal(signal.SIGTERM)
                except ProcessLookupError:
                    return harbor.wait()
                try:
                    return harbor.wait(timeout=grace_s)
                except subprocess.TimeoutExpired:
                    os.killpg(os.getpgrp(), signal.SIGKILL)
                    return harbor.wait()
            return harbor.wait()
        finally:
            os.close(lifeline_fd)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lifeline-fd", type=int, required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    arguments = parser.parse_args(argv)
    command = arguments.command
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        parser.error("Harbor command is required after --lifeline-fd")
    return run_guard(command, arguments.lifeline_fd)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
