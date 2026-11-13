from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import threading
from pathlib import Path

from harbor_research_runner.docker_host.lifeline import watch_lifeline
from harbor_research_runner.records.jsonl import append_row, open_for_append

DOCKER_EVENTS_COMMAND = [
    "docker",
    "events",
    "--filter",
    "type=container",
    "--filter",
    "event=oom",
    "--format",
    "{{json .}}",
]


def append_event_line(fd: int, line: str) -> None:
    row = json.loads(line)
    if not isinstance(row, dict):
        raise ValueError("Docker event must be a JSON object")
    append_row(fd, row)


def _pump(stdout, fd: int, failed: threading.Event) -> None:
    try:
        for line in stdout:
            append_event_line(fd, line)
    except BaseException:
        failed.set()
        raise


def run_oom_watch(out: Path, lifeline_fd: int | None = None) -> int:
    stopped = threading.Event()
    failed = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stopped.set())
    if lifeline_fd is not None:
        threading.Thread(target=watch_lifeline, args=(lifeline_fd, stopped), daemon=True).start()
    out.parent.mkdir(parents=True, exist_ok=True)
    fd = open_for_append(out)
    process = subprocess.Popen(
        DOCKER_EVENTS_COMMAND,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
    )
    reader = threading.Thread(target=_pump, args=(process.stdout, fd, failed), daemon=True)
    reader.start()
    try:
        while not stopped.wait(0.2):
            if failed.is_set():
                return 1
            if process.poll() is not None:
                reader.join(timeout=1)
                return process.returncode or 1
        return 0
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait()
        reader.join(timeout=1)
        os.close(fd)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--lifeline-fd", type=int)
    args = parser.parse_args(argv)
    return run_oom_watch(args.out, args.lifeline_fd)


if __name__ == "__main__":
    raise SystemExit(main())
