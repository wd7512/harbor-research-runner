from __future__ import annotations

import hashlib
import logging
import os
import shlex
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from harbor_research_runner.harbor_adapter import commands, job_folder, set_aside
from harbor_research_runner.harness.agents import agent_import_path, agent_settings
from harbor_research_runner.harness.manifest import ImageEntry, load_manifest
from harbor_research_runner.job_config.job_file import Arm, Job, TaskSource, load_job, planned_slots
from harbor_research_runner.job_config.model_cards import ModelCard, load_card
from harbor_research_runner.records import jsonl, run_metadata
from harbor_research_runner.records.limits import Limits
from harbor_research_runner.records.machine_check import MachineCheck
from harbor_research_runner.records.proxy_control import ProxyConfig
from harbor_research_runner.records.proxy_control import write as write_proxy_config
from harbor_research_runner.records.run_events import (
    Finish,
    HarborExit,
    HarborStart,
    Launch,
    LaunchCheck,
    Resume,
)
from harbor_research_runner.run import canary, process_group, watch
from harbor_research_runner.run.checks import CheckDeps, CheckResult, resolve_tasks, run_checks
from harbor_research_runner.run.tool_identity import ToolIdentity
from harbor_research_runner.run.watch import WatchResult
from harbor_research_runner.trial_validity.verdict import Verdict

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SlotsStatus:
    complete: list[tuple[str, str]]
    incomplete: list[tuple[str, str]]
    to_set_aside: list[str]
    unmeasured_final: list[str]


@dataclass(frozen=True)
class RunDeps:
    checks: CheckDeps
    tool: ToolIdentity
    host: Callable[[], dict]
    start_harbor: Callable[[list[str], Path], Any]
    watch: Callable[[RunSession, Any, Path, float], WatchResult]
    start_services: Callable[[RunSession, bool], None]
    stop_services: Callable[[RunSession], None]
    now: Callable[[], float]


class RefusedToStart(Exception):
    pass


class RunSession:
    def __init__(
        self,
        run_dir: Path,
        job: Job,
        card: ModelCard,
        limits: Limits,
        metadata: run_metadata.RunMetadata,
        environment: Mapping[str, str],
        tool: ToolIdentity,
        manifest_path: Path | None = None,
    ) -> None:
        self.run_dir = run_dir
        self.job = job
        self.card = card
        self.limits = limits
        self.metadata = metadata
        self.environment = dict(environment)
        self.tool = tool
        self.manifest_path = manifest_path
        self._sampler: subprocess.Popen | None = None
        self._proxy: subprocess.Popen | None = None

    @classmethod
    def create(
        cls,
        job: Job,
        card: ModelCard,
        manifest_path: Path,
        runs_dir: Path,
        environment: Mapping[str, str],
        check_results: list[CheckResult],
        tasks: dict,
        arms: list[dict],
        machine_check: MachineCheck,
        now,
        tool: ToolIdentity,
        host: dict,
    ) -> RunSession:
        expected_host_fields = {"fingerprint", "arch", "cpus", "memory_mb", "docker_version"}
        if set(host) != expected_host_fields:
            missing = sorted(expected_host_fields - set(host))
            extra = sorted(set(host) - expected_host_fields)
            raise ValueError(f"host fields differ: missing={missing}, extra={extra}")
        created_t = now()
        timestamp = datetime.fromtimestamp(created_t, UTC).strftime("%Y%m%dT%H%M%S%fZ")
        run_id = f"{job.name}-{timestamp}"
        run_dir = runs_dir / run_id
        run_dir.mkdir(parents=True, exist_ok=False)
        limits = Limits.from_dict(
            job.limit_overrides, job.repetitions, machine_check.verifier_timeout_multiplier
        )
        _append_event(run_dir, Launch(created_t, tool.version, tool.commit, os.getpid()))
        for result in check_results:
            _append_event(run_dir, LaunchCheck(now(), result.name, result.ok, result.detail))
        if any(not result.ok for result in check_results):
            raise RefusedToStart("one or more launch checks failed")
        metadata = run_metadata.RunMetadata(
            run_id=run_id,
            created_t=created_t,
            tool_version=tool.version,
            tool_commit=tool.commit,
            harbor_version=machine_check.harbor_version,
            job=_job_to_row(job),
            job_file_sha256=hashlib.sha256(job.path.read_bytes()).hexdigest(),
            model_card=_card_to_row(card),
            arms=arms,
            task_set=tasks,
            machine_check_sha256=hashlib.sha256(job.machine_check.read_bytes()).hexdigest(),
            host=dict(host),
            proxy={"host": job.proxy_host, "port": job.proxy_port},
            limits=limits.to_row(),
        )
        run_metadata.write_once(run_dir / "run-metadata.json", metadata)
        return cls(run_dir, job, card, limits, metadata, environment, tool, manifest_path)

    @classmethod
    def load(cls, run_dir: Path, environment: Mapping[str, str]) -> RunSession:
        metadata = run_metadata.read(run_dir / "run-metadata.json")
        job = _job_from_row(metadata.job)
        card = ModelCard(**metadata.model_card)
        limits = Limits.from_row(metadata.limits)
        return cls(
            run_dir,
            job,
            card,
            limits,
            metadata,
            environment,
            ToolIdentity(metadata.tool_version, metadata.tool_commit),
        )

    def mark_resumed(self, now) -> None:
        self.event(Resume(now(), self.tool.version, self.tool.commit, os.getpid()))

    def event(self, event) -> None:
        _append_event(self.run_dir, event)

    def start_services(self, restart: bool) -> None:
        config = ProxyConfig(
            host=self.job.proxy_host,
            port=self.job.proxy_port,
            ledger_path=str(self.run_dir / "proxy-ledger.jsonl"),
            bodies_dir=str(self.run_dir / "request-bodies"),
            model=_card_to_row(self.card),
            limits=self.limits.to_row(),
            restart_cooldown_s=60.0 if restart else 0.0,
        )
        config_path = self.run_dir / "proxy-config.json"
        write_proxy_config(config_path, config)
        environment = os.environ.copy()
        environment.update(
            {name: value for name, value in self.environment.items() if name in self.card.key_env}
        )
        try:
            self._sampler = process_group.spawn_group(
                [
                    "uv",
                    "run",
                    "python",
                    "-m",
                    "harbor_research_runner.docker_host.sampler",
                    "--out",
                    str(self.run_dir / "resource-stats.jsonl"),
                ],
                env=environment,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.STDOUT,
            )
            self._proxy = process_group.spawn_group(
                [
                    "uv",
                    "run",
                    "python",
                    "-m",
                    "harbor_research_runner.model_proxy",
                    "--config",
                    str(config_path),
                ],
                env=environment,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.STDOUT,
            )
        except BaseException:
            self.stop_services()
            raise
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if not self.proxy_alive():
                self.stop_services()
                raise RuntimeError("proxy exited before becoming healthy")
            try:
                with urllib.request.urlopen(
                    f"http://{self.job.proxy_host}:{self.job.proxy_port}/control/health",
                    timeout=0.2,
                ) as response:
                    if response.status == 200:
                        return
            except (OSError, urllib.error.URLError):
                pass
            time.sleep(0.5)
        self.stop_services()
        raise TimeoutError("proxy did not become healthy within 30 seconds")

    def stop_services(self) -> None:
        for name in ("_proxy", "_sampler"):
            process = getattr(self, name)
            if process is not None and process.poll() is None:
                process_group.stop_group(process, grace_s=15)
            setattr(self, name, None)

    def proxy_alive(self) -> bool:
        return self._proxy is not None and self._proxy.poll() is None


def slots_status(
    planned: list[tuple[str, str]],
    repetitions: int,
    verdict_events: list[dict],
    set_aside_events: list[dict],
    in_job: set[str],
    limits: Limits,
) -> SlotsStatus:
    verdicts = {
        row["trial"]: row
        for row in verdict_events
        if row.get("type") == "trial_verdict" and isinstance(row.get("trial"), str)
    }
    valid_counts: dict[tuple[str, str], int] = {}
    set_aside_counts: dict[tuple[str, str], int] = {}
    for event in set_aside_events:
        if event.get("type") == "set_aside":
            slot = (event["task"], event["arm"])
            set_aside_counts[slot] = set_aside_counts.get(slot, 0) + 1

    to_set_aside: list[str] = []
    unmeasured_final: list[str] = []
    for trial in sorted(in_job):
        event = verdicts.get(trial)
        if event is None:
            continue
        slot = (event["task"], event["arm"])
        if event["valid"]:
            valid_counts[slot] = valid_counts.get(slot, 0) + 1
            if not event["measured"]:
                unmeasured_final.append(trial)
        elif set_aside_counts.get(slot, 0) < limits.max_set_asides_per_slot:
            to_set_aside.append(trial)
            set_aside_counts[slot] = set_aside_counts.get(slot, 0) + 1

    complete = [slot for slot in planned if valid_counts.get(slot, 0) >= repetitions]
    complete_set = set(complete)
    return SlotsStatus(
        complete=complete,
        incomplete=[slot for slot in planned if slot not in complete_set],
        to_set_aside=to_set_aside,
        unmeasured_final=unmeasured_final,
    )


def run_job(
    job_path: Path,
    runs_dir: Path,
    manifest_path: Path,
    environment: Mapping[str, str],
    deps: RunDeps,
) -> int:
    host_lock = deps.checks.host_lock
    try:
        host_lock.acquire()
    except Exception as exc:
        logger.error("run refused because the host lock could not be acquired: %s", exc)
        return 1
    session = None
    services_started = False
    checks: list[CheckResult] = []
    try:
        job = load_job(job_path)
        card = load_card(job.model, job_path.parent)
        manifest = load_manifest(manifest_path)
        host = deps.host()
        machine_check = deps.checks.read_machine_check(job.machine_check)
        checks = run_checks(
            job,
            card,
            manifest,
            host["arch"],
            runs_dir,
            environment,
            deps.checks,
        )
        task_names = resolve_tasks(job, machine_check)
        session = RunSession.create(
            job=job,
            card=card,
            manifest_path=manifest_path,
            runs_dir=runs_dir,
            environment=environment,
            check_results=checks,
            tasks=_task_set(job, task_names, machine_check),
            arms=_arm_rows(job, manifest, host["arch"]),
            machine_check=machine_check,
            now=deps.now,
            tool=deps.tool,
            host=host,
        )
        deps.start_services(session, False)
        services_started = True
        agents = _agent_configs(job, card, manifest_path)
        canary_config = canary.canary_job_config(
            job, agents, machine_check.verifier_timeout_multiplier
        )
        commands.write_job_config(session.run_dir / "canary-job-config.yaml", canary_config)
        canary_argv = commands.run_command(
            session.run_dir, "canary-job", _proxy_url(job), job.task_order
        )
        canary_process = deps.start_harbor(canary_argv, session.run_dir)
        session.event(HarborStart(deps.now(), 0, shlex.join(canary_argv), "canary"))
        canary_returncode = canary_process.wait()
        session.event(HarborExit(deps.now(), 0, canary_returncode, "canary"))
        canary_ok, canary_detail = _canary_passed(session, card, job, deps.now)
        if canary_returncode != 0 or not canary_ok:
            logger.error(
                "canary failed with Harbor exit code %s: %s", canary_returncode, canary_detail
            )
            deps.stop_services(session)
            services_started = False
            return 1

        main_config = commands.job_config(
            job,
            task_names,
            agents,
            machine_check.verifier_timeout_multiplier,
        )
        commands.write_job_config(session.run_dir / "harbor-job-config.yaml", main_config)
        result = _pass_loop(session, planned_slots(job, task_names), deps, 0)
        services_started = False
        return result
    except RefusedToStart as exc:
        failed = [f"{item.name}: {item.detail}" for item in checks if not item.ok]
        logger.error("run refused by launch checks: %s", "; ".join(failed) or exc)
        return 1
    except Exception:
        logger.exception("run failed")
        return 1
    finally:
        if session is not None and services_started:
            deps.stop_services(session)
        host_lock.release()


def resume_run(
    run_id: str,
    runs_dir: Path,
    manifest_path: Path,
    environment: Mapping[str, str],
    deps: RunDeps,
) -> int:
    host_lock = deps.checks.host_lock
    try:
        host_lock.acquire()
    except Exception as exc:
        logger.error("resume refused because the host lock could not be acquired: %s", exc)
        return 1
    session = None
    services_started = False
    try:
        run_dir = runs_dir / run_id
        session = RunSession.load(run_dir, environment)
        events = jsonl.read_rows(run_dir / "run-events.jsonl").rows
        if events and events[-1].get("type") == "finish":
            logger.error("resume refused because run %s is already finished", run_id)
            return 1
        manifest = load_manifest(manifest_path)
        host = deps.host()
        checks = run_checks(
            session.job,
            session.card,
            manifest,
            host["arch"],
            runs_dir,
            environment,
            deps.checks,
        )
        failed = [f"{result.name}: {result.detail}" for result in checks if not result.ok]
        if failed:
            logger.error("resume refused by launch checks: %s", "; ".join(failed))
            return 1
        deps.start_services(session, True)
        services_started = True
        session.mark_resumed(deps.now)
        fd = jsonl.open_for_append(run_dir / "run-events.jsonl")
        try:
            set_aside.repair(run_dir, fd, deps.now())
        finally:
            os.close(fd)
        main_dir = run_dir / "harbor-job"
        judged = watch.judged_trials(run_dir)
        watch.judge_all_remaining(session, main_dir, judged, deps.now())
        task_names = [row["name"] for row in session.metadata.task_set["tasks"]]
        planned = planned_slots(session.job, task_names)
        status = _current_slots_status(session, planned)
        if status.to_set_aside:
            _set_aside_trials(session, status, deps.now)
        trials_left = bool(status.incomplete) and len(job_folder.trial_folders(main_dir)) < (
            len(planned) * session.job.repetitions
        )
        if not status.to_set_aside and not trials_left:
            result = _finish(session, status, deps)
            services_started = False
            return result
        starts = [
            row for row in events if row.get("type") == "harbor_start" and row.get("job") == "main"
        ]
        result = _pass_loop(session, planned, deps, len(starts) or 1)
        services_started = False
        return result
    except Exception:
        logger.exception("resume failed")
        return 1
    finally:
        if session is not None and services_started:
            deps.stop_services(session)
        host_lock.release()


def _pass_loop(
    session: RunSession, planned: list[tuple[str, str]], deps: RunDeps, pass_number: int
) -> int:
    while True:
        proxy_url = _proxy_url(session.job)
        if pass_number == 0:
            argv = commands.run_command(
                session.run_dir, "harbor-job", proxy_url, session.job.task_order
            )
        else:
            argv = commands.resume_command(
                session.run_dir, "harbor-job", proxy_url, session.job.task_order
            )
        process = deps.start_harbor(argv, session.run_dir)
        session.event(HarborStart(deps.now(), pass_number, shlex.join(argv), "main"))
        result = deps.watch(session, process, session.run_dir / "harbor-job", deps.now())
        returncode = result.returncode if result.returncode is not None else process.wait()
        session.event(HarborExit(deps.now(), pass_number, returncode, "main"))
        if result.kind == "halted":
            judged = watch.judged_trials(session.run_dir)
            watch.judge_all_remaining(session, session.run_dir / "harbor-job", judged, deps.now())
            deps.stop_services(session)
            return 2
        judged = watch.judged_trials(session.run_dir)
        watch.judge_all_remaining(session, session.run_dir / "harbor-job", judged, deps.now())
        status = _current_slots_status(session, planned)
        if not status.to_set_aside:
            return _finish(session, status, deps)
        _set_aside_trials(session, status, deps.now)
        pass_number += 1


def _current_slots_status(session, planned: list[tuple[str, str]]) -> SlotsStatus:
    events = jsonl.read_rows(session.run_dir / "run-events.jsonl").rows
    in_job = {path.name for path in job_folder.trial_folders(session.run_dir / "harbor-job")}
    return slots_status(
        planned,
        session.job.repetitions,
        events,
        events,
        in_job,
        session.limits,
    )


def _set_aside_trials(session, status: SlotsStatus, now: Callable[[], float]) -> None:
    events = jsonl.read_rows(session.run_dir / "run-events.jsonl").rows
    verdicts = {row["trial"]: row for row in events if row.get("type") == "trial_verdict"}
    fd = jsonl.open_for_append(session.run_dir / "run-events.jsonl")
    try:
        set_aside.repair(session.run_dir, fd, now())
        for trial in status.to_set_aside:
            verdict = verdicts[trial]
            set_aside.set_aside(
                session.run_dir,
                trial,
                verdict["task"],
                verdict["arm"],
                verdict["reason"],
                fd,
                now(),
            )
    finally:
        os.close(fd)


def _finish(session, status: SlotsStatus, deps: RunDeps) -> int:
    deps.stop_services(session)
    exit_code = 0 if not status.incomplete and not status.unmeasured_final else 3
    session.event(Finish(deps.now(), exit_code, len(status.complete), len(status.incomplete)))
    make_read_only(session.run_dir)
    return exit_code


def make_read_only(run_dir: Path) -> None:
    for path in sorted(run_dir.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        if path.is_symlink():
            continue
        mode = path.stat().st_mode
        path.chmod(mode & ~0o222)
    mode = run_dir.stat().st_mode
    run_dir.chmod(mode & ~0o222)


def _canary_passed(
    session, card: ModelCard, job: Job, now: Callable[[], float]
) -> tuple[bool, str]:
    canary_dir = session.run_dir / "canary-job"
    watch.judge_all_remaining(session, canary_dir, watch.judged_trials(session.run_dir), now())
    facts = [job_folder.read_trial(path) for path in job_folder.trial_folders(canary_dir)]
    events = jsonl.read_rows(session.run_dir / "run-events.jsonl").rows
    verdict_events = {
        row["trial"]: Verdict(
            row["valid"],
            row["reason"],
            row["instrument"],
            tuple(row["labels"]),
            row["measured"],
            row["unmeasured_reason"],
        )
        for row in events
        if row.get("type") == "trial_verdict"
    }
    if any(fact.trial not in verdict_events for fact in facts):
        return False, "canary trial is missing a verdict"
    verdicts = [verdict_events[fact.trial] for fact in facts]
    ledger_rows = jsonl.read_rows(session.run_dir / "proxy-ledger.jsonl").rows
    passed, _detail = canary.canary_passed(
        facts, verdicts, ledger_rows, card, [arm.label for arm in job.arms]
    )
    return passed, _detail


def _agent_configs(job: Job, card: ModelCard, manifest_path: Path) -> list[tuple[str, dict]]:
    agents = []
    for arm in job.arms:
        import_path = agent_import_path(arm.harness)
        settings = agent_settings(
            arm.label,
            arm.harness,
            arm.commit,
            str(manifest_path),
            job.container_host,
            job.proxy_port,
            card.default_variant,
            model_card=asdict(card),
            fake=arm.fake,
        )
        agents.append((import_path, settings))
    return agents


def _task_set(job: Job, task_names: list[str], machine_check: MachineCheck) -> dict:
    task_rows = {row["name"]: row for row in machine_check.tasks}
    return {
        "ref": _task_ref(job),
        "tasks": [{"name": name, "digest": task_rows[name]["digest"]} for name in task_names],
    }


def _task_ref(job: Job) -> str:
    if job.tasks.kind == "dataset" and job.tasks.dataset is not None:
        return job.tasks.dataset
    if job.tasks.kind == "path" and job.tasks.path is not None:
        return job.tasks.path
    if (
        job.tasks.kind == "git"
        and job.tasks.git_url is not None
        and job.tasks.git_commit is not None
    ):
        return f"{job.tasks.git_url}@{job.tasks.git_commit}"
    raise ValueError("job task source is incomplete")


def _arm_rows(job: Job, manifest: dict[tuple[str, str, str], ImageEntry], arch: str) -> list[dict]:
    rows = []
    for arm in job.arms:
        entry = manifest.get((arm.harness, arm.commit, arch))
        rows.append(
            {
                "label": arm.label,
                "harness": arm.harness,
                "commit": arm.commit,
                "image_ref": entry.ref if entry is not None else "",
                "image_digest": entry.digest if entry is not None else "",
                "build_method": entry.build_method if entry is not None else "",
            }
        )
    return rows


def _proxy_url(job: Job) -> str:
    return f"http://{job.proxy_host}:{job.proxy_port}"


def _job_to_row(job: Job) -> dict:
    row = asdict(job)
    row["path"] = str(job.path)
    row["machine_check"] = str(job.machine_check)
    return row


def _append_event(run_dir: Path, event) -> None:
    fd = jsonl.open_for_append(run_dir / "run-events.jsonl")
    try:
        jsonl.append_row(fd, event.to_row())
    finally:
        os.close(fd)


def _job_from_row(row: dict) -> Job:
    values = dict(row)
    task_values = dict(values["tasks"])
    task_values["include"] = tuple(task_values["include"])
    task_values["exclude"] = tuple(task_values["exclude"])
    values["tasks"] = TaskSource(**task_values)
    values["arms"] = tuple(Arm(**arm) for arm in values["arms"])
    values["path"] = Path(values["path"])
    values["machine_check"] = Path(values["machine_check"])
    return Job(**values)


def _card_to_row(card: ModelCard) -> dict:
    return asdict(card)
