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


def run_sampler(out: Path, interval: float) -> None:
    stopped = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stopped.set())
    out.parent.mkdir(parents=True, exist_ok=True)
    fd = open_for_append(out)
    last = Sample(0.0, 0, 0, 0.0, 0.0, 0, False)
    try:
        while not stopped.is_set():
            try:
                measured = measure()
                if measured.docker_ok:
                    last = measured
                else:
                    last = Sample(
                        t=measured.t,
                        mem_available_mb=last.mem_available_mb,
                        swap_used_mb=last.swap_used_mb,
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
                    swap_used_mb=last.swap_used_mb,
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
    args = parser.parse_args(argv)
    if args.interval <= 0:
        parser.error("--interval must be positive")
    run_sampler(args.out, args.interval)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
