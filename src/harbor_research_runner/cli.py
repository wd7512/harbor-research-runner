from __future__ import annotations

import argparse
import logging
import os
import time
from collections.abc import Callable, Mapping
from importlib.metadata import version
from pathlib import Path

from harbor_research_runner.docker_host import cleanup, resources
from harbor_research_runner.docker_host.host_lock import HostLock
from harbor_research_runner.evidence_bundle import build as evidence_build
from harbor_research_runner.harness import build as harness_build
from harbor_research_runner.job_config.job_file import load_job
from harbor_research_runner.model_proxy.adapters import select_adapter
from harbor_research_runner.records.machine_check import read
from harbor_research_runner.results_explorer.loader import EvidenceStore
from harbor_research_runner.results_explorer.server import build_server
from harbor_research_runner.run import loop, machine_check, process_group, watch
from harbor_research_runner.run.checks import CheckDeps
from harbor_research_runner.run.machine_check import MachineCheckDeps
from harbor_research_runner.run.tool_identity import tool_identity

logger = logging.getLogger(__name__)
Steps = Mapping[str, Callable]


def _run_id(value: str) -> str:
    if not value or value in {".", ".."} or Path(value).name != value:
        raise argparse.ArgumentTypeError(f"run id must be one folder name, got {value!r}")
    return value


def _absolute_path(value: str) -> Path:
    return Path(value).resolve()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hrr")
    parser.add_argument("--version", action="version", version=version("harbor-research-runner"))
    commands = parser.add_subparsers(dest="command", required=True)

    build_harness = commands.add_parser("build-harness")
    build_harness.add_argument("--harness", required=True)
    build_harness.add_argument("--commit", required=True)
    build_harness.add_argument("--manifest", type=_absolute_path, default="harness-manifest.toml")
    build_harness.add_argument("--arch", choices=("amd64", "arm64"))

    check = commands.add_parser("check-machine")
    check.add_argument("--job", type=_absolute_path, required=True)
    check.add_argument("--out", type=_absolute_path, required=True)
    check.add_argument("--with-do-nothing", action="store_true")
    check.add_argument("--runs-dir", type=_absolute_path, default="runs")

    run = commands.add_parser("run")
    run_source = run.add_mutually_exclusive_group(required=True)
    run_source.add_argument("job", type=_absolute_path, nargs="?")
    run_source.add_argument("--resume", type=_run_id)
    run.add_argument("--runs-dir", type=_absolute_path, default="runs")
    run.add_argument("--evidence-dir", type=_absolute_path, default="evidence")
    run.add_argument("--manifest", type=_absolute_path, default="harness-manifest.toml")
    run.add_argument("--no-evidence", action="store_true")

    evidence = commands.add_parser("build-evidence")
    evidence.add_argument("run_id", type=_run_id)
    evidence.add_argument("--runs-dir", type=_absolute_path, default="runs")
    evidence.add_argument("--evidence-dir", type=_absolute_path, default="evidence")

    explore = commands.add_parser("explore")
    explore.add_argument(
        "paths", type=_absolute_path, nargs="*", default=[_absolute_path("evidence")]
    )
    explore.add_argument("--port", type=int, default=8780)

    clean = commands.add_parser("clean-docker")
    clean.add_argument("--apply", action="store_true")
    clean.add_argument("--runs-dir", type=_absolute_path, default="runs")
    return parser


def _check_deps() -> CheckDeps:
    return CheckDeps(
        select_adapter=select_adapter,
        measure=resources.measure,
        inventory=cleanup.inventory,
        live_run=cleanup.live_run,
        host_lock=HostLock(),
        read_machine_check=read,
        host_fingerprint=resources.host_fingerprint,
        host_profile=resources.host_profile,
        harbor_version=lambda: version("harbor"),
        docker_memory_mb=cleanup.docker_memory_mb,
    )


def _run_deps() -> loop.RunDeps:
    return loop.RunDeps(
        checks=_check_deps(),
        tool=tool_identity(),
        host=resources.host_description,
        start_harbor=_start_harbor,
        watch=watch.watch,
        start_services=lambda session, restart: session.start_services(restart),
        stop_services=lambda session: session.stop_services(),
        now=time.time,
    )


def _start_harbor(argv: list[str], cwd: Path, lifeline_fd: int):
    command = [
        "uv",
        "run",
        "python",
        "-m",
        "harbor_research_runner.run.harbor_guard",
        "--lifeline-fd",
        str(lifeline_fd),
        "--",
        *argv,
    ]
    return process_group.spawn_group(
        command,
        env=os.environ.copy(),
        cwd=cwd,
        pass_fds=(lifeline_fd,),
    )


def _run_harbor(argv: list[str], cwd: Path) -> int:
    return process_group.spawn_group(argv, env=os.environ.copy(), cwd=cwd).wait()


def _step(steps: Steps | None, name: str, default: Callable) -> Callable:
    return default if steps is None else steps.get(name, default)


def _check_machine(args: argparse.Namespace, steps: Steps | None) -> int:
    checks = _step(steps, "check_deps", _check_deps)()
    deps = MachineCheckDeps(checks=checks, run_harbor=_run_harbor, now=time.time)
    checker = _step(steps, "check_machine", machine_check.run_machine_check)
    try:
        result = checker(load_job(args.job), args.out, args.with_do_nothing, args.runs_dir, deps)
    except Exception as exc:
        logger.error("machine check failed: %s", exc)
        return 1
    finally:
        checks.host_lock.release()
    logger.info(
        "machine check included %d task(s): %s", len(result.included), ", ".join(result.included)
    )
    return 0


def _run(args: argparse.Namespace, steps: Steps | None) -> int:
    environment = dict(os.environ)
    evidence_builder = _step(steps, "build_evidence", evidence_build.build)
    if args.resume is None:
        runner = _step(steps, "run_job", loop.run_job)
        outcome = runner(
            args.job,
            args.runs_dir,
            args.manifest,
            environment,
            _step(steps, "run_deps", _run_deps)(),
        )
    else:
        runner = _step(steps, "resume_run", loop.resume_run)
        outcome = runner(
            args.resume,
            args.runs_dir,
            args.manifest,
            environment,
            _step(steps, "run_deps", _run_deps)(),
        )
    if not args.no_evidence and outcome.exit_code in {0, 3} and outcome.run_dir is not None:
        try:
            evidence_result = evidence_builder(
                outcome.run_dir, args.evidence_dir / outcome.run_dir.name, environment
            )
        except Exception:
            logger.exception("evidence build failed for run %s", outcome.run_dir.name)
        else:
            if evidence_result == 4:
                return 4
            if evidence_result != 0:
                logger.error(
                    "evidence build failed for run %s with exit code %s",
                    outcome.run_dir.name,
                    evidence_result,
                )
    return outcome.exit_code


def _serve(args: argparse.Namespace, steps: Steps | None) -> int:
    store = EvidenceStore(args.paths)
    server = _step(steps, "build_server", build_server)(store, args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def _clean_docker(args: argparse.Namespace, steps: Steps | None) -> int:
    cleaner = _step(steps, "clean_docker", cleanup.clean)
    try:
        report = cleaner(args.apply, HostLock(), runs_dir=args.runs_dir)
    except Exception as exc:
        logger.error("Docker cleanup failed: %s", exc)
        return 1
    for name in report.planned_containers:
        logger.info("planned container: %s", name)
    for name in report.planned_networks:
        logger.info("planned network: %s", name)
    for name in report.skipped_attached:
        logger.info("skipped attached network: %s", name)
    if args.apply:
        for name in report.removed_containers:
            logger.info("removed container: %s", name)
        for name in report.removed_networks:
            logger.info("removed network: %s", name)
    if report.planned_containers or report.planned_networks or report.skipped_attached:
        return 1 if not args.apply else 0
    return 0


def main(argv: list[str] | None = None, steps: Steps | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    args = _parser().parse_args(argv)
    if args.command == "build-harness":
        try:
            arch = _step(steps, "resolve_arch", harness_build.resolve_arch)(args.arch)
            _step(steps, "build_harness", harness_build.build)(
                args.harness, args.commit, args.manifest, arch
            )
        except Exception as exc:
            logger.error("harness build failed: %s", exc)
            return 1
        return 0
    if args.command == "check-machine":
        return _check_machine(args, steps)
    if args.command == "run":
        return _run(args, steps)
    if args.command == "build-evidence":
        try:
            return _step(steps, "build_evidence", evidence_build.build)(
                args.runs_dir / args.run_id, args.evidence_dir / args.run_id, dict(os.environ)
            )
        except Exception as exc:
            logger.error("evidence build failed: %s", exc)
            return 1
    if args.command == "explore":
        return _serve(args, steps)
    if args.command == "clean-docker":
        return _clean_docker(args, steps)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
