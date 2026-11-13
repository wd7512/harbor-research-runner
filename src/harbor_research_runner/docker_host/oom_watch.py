from __future__ import annotations

import argparse
import json
import os
import select
import signal
import subprocess
import threading
from pathlib import Path

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


def _watch_lifeline(fd: int, stopped: threading.Event) -> None:
    try:
        while os.read(fd, 1):
            pass
    except OSError:
        pass
    finally:
        os.close(fd)
        stopped.set()


def run_oom_watch(out: Path, lifeline_fd: int | None = None) -> int:
    stopped = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stopped.set())
    if lifeline_fd is not None:
        threading.Thread(target=_watch_lifeline, args=(lifeline_fd, stopped), daemon=True).start()
    out.parent.mkdir(parents=True, exist_ok=True)
    fd = open_for_append(out)
    process = subprocess.Popen(
        DOCKER_EVENTS_COMMAND,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
    )
    try:
        if process.stdout is None:
            raise RuntimeError("Docker event stream has no stdout")
        while not stopped.is_set():
            ready, _, _ = select.select([process.stdout], [], [], 0.2)
            if ready:
                line = process.stdout.readline()
                if not line:
                    returncode = process.poll()
                    if returncode is not None:
                        return returncode if returncode != 0 else 1
                    continue
                append_event_line(fd, line)
                continue
            returncode = process.poll()
            if returncode is not None:
                return returncode if returncode != 0 else 1
        process.terminate()
        process.wait()
        return 0
    except BaseException:
        process.terminate()
        process.wait()
        raise
    finally:
        if process.stdout is not None:
            process.stdout.close()
        os.close(fd)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--lifeline-fd", type=int)
    args = parser.parse_args(argv)
    return run_oom_watch(args.out, args.lifeline_fd)


if __name__ == "__main__":
    raise SystemExit(main())
