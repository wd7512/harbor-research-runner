from __future__ import annotations

import argparse
import logging
import os
import signal
import threading
import time
from pathlib import Path

from harbor_research_runner.docker_host.resources import measure
from harbor_research_runner.records.jsonl import append_row, open_for_append
from harbor_research_runner.records.resource_stats import Sample

logger = logging.getLogger(__name__)


def _watch_lifeline(fd: int, stopped: threading.Event) -> None:
    try:
        while os.read(fd, 1):
            pass
    except OSError:
        pass
    finally:
        os.close(fd)
        stopped.set()


def run_sampler(out: Path, interval: float, lifeline_fd: int | None = None) -> None:
    stopped = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stopped.set())
    if lifeline_fd is not None:
        threading.Thread(target=_watch_lifeline, args=(lifeline_fd, stopped), daemon=True).start()
    out.parent.mkdir(parents=True, exist_ok=True)
    fd = open_for_append(out)
    last = Sample(0.0, 0, 0, 0.0, 0.0, 0, False)
    previous_swapouts = None
    try:
        while not stopped.is_set():
            try:
                measured, previous_swapouts = measure(previous_swapouts)
                if measured.docker_ok:
                    last = measured
                else:
                    last = Sample(
                        t=measured.t,
                        mem_available_mb=last.mem_available_mb,
                        swapped_out_pages=measured.swapped_out_pages,
                        disk_free_gb=last.disk_free_gb,
                        load1=last.load1,
                        trial_containers=last.trial_containers,
                        docker_ok=False,
                    )
            except Exception:
                logger.exception("host resource measurement failed")
                last = Sample(
                    t=time.time(),
                    mem_available_mb=last.mem_available_mb,
                    swapped_out_pages=0,
                    disk_free_gb=last.disk_free_gb,
                    load1=last.load1,
                    trial_containers=last.trial_containers,
                    docker_ok=False,
                )
            append_row(fd, last.to_row())
            stopped.wait(interval)
    finally:
        os.close(fd)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--interval", type=float, default=10)
    parser.add_argument("--lifeline-fd", type=int)
    args = parser.parse_args(argv)
    if args.interval <= 0:
        parser.error("--interval must be positive")
    run_sampler(args.out, args.interval, args.lifeline_fd)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
