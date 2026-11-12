from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from importlib.metadata import version
from pathlib import Path

from harbor_research_runner.docker_host.cleanup import inventory
from harbor_research_runner.docker_host.host_lock import HostLock
from harbor_research_runner.docker_host.resources import host_fingerprint, measure
from harbor_research_runner.job_config.job_file import load_job
from harbor_research_runner.records.machine_check import read
from harbor_research_runner.run.checks import CheckDeps
from harbor_research_runner.run.machine_check import MachineCheckDeps, run_machine_check
from harbor_research_runner.run.process_group import spawn_group

logger = logging.getLogger(__name__)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hrr")
    commands = parser.add_subparsers(dest="command", required=True)
    check_machine = commands.add_parser("check-machine")
    check_machine.add_argument("--job", type=Path, required=True)
    check_machine.add_argument("--out", type=Path, required=True)
    return parser


def _run_harbor(argv: list[str], cwd: Path) -> int:
    process = spawn_group(argv, env=os.environ.copy(), cwd=cwd)
    return process.wait()


def _check_machine(job_path: Path, out: Path) -> int:
    job = load_job(job_path)
    host_lock = HostLock()
    checks = CheckDeps(
        select_adapter=lambda _api, _path: None,
        measure=measure,
        inventory=inventory,
        live_run=lambda _runs_dir: None,
        host_lock=host_lock,
        read_machine_check=read,
        host_fingerprint=host_fingerprint,
        harbor_version=lambda: version("harbor"),
    )
    deps = MachineCheckDeps(
        checks=checks,
        run_harbor=_run_harbor,
        now=time.time,
    )
    try:
        check = run_machine_check(job, out, True, Path("runs").resolve(), deps)
    except Exception as exc:
        logger.error("machine check failed: %s", exc)
        return 1
    finally:
        host_lock.release()
    logger.info(
        "machine check included %d task(s): %s", len(check.included), ", ".join(check.included)
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    args = _parser().parse_args(argv)
    if args.command == "check-machine":
        return _check_machine(args.job, args.out)
    sys.stderr.write(f"unknown command: {args.command}\n")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
