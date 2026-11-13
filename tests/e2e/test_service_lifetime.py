from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
from contextlib import suppress
from pathlib import Path
from types import SimpleNamespace

from harbor_research_runner.job_config.job_file import Arm, Job, TaskSource
from harbor_research_runner.job_config.model_cards import ModelCard
from harbor_research_runner.records.limits import Limits
from harbor_research_runner.run.loop import RunSession
from harbor_research_runner.run.tool_identity import ToolIdentity

ROOT = Path(__file__).resolve().parents[2]


def _service_helper(run_dir: Path, port: int, pids_path: Path) -> int:
    run_dir.mkdir(parents=True, exist_ok=True)
    job = Job(
        path=run_dir / "job.yaml",
        name="service-lifetime",
        model="fake/model",
        repetitions=1,
        concurrency=1,
        task_order="as_listed",
        tasks=TaskSource("dataset", "bench/tasks", None, None, None, (), ()),
        machine_check=run_dir / "machine-check.json",
        arms=(Arm("arm-a", "fake", "a" * 40),),
        proxy_host="127.0.0.1",
        proxy_port=port,
        container_host="host.docker.internal",
        limit_overrides={},
    )
    card = ModelCard(
        id="fake/model",
        upstream_model="fake-model",
        api="chat",
        endpoints=["http://127.0.0.1:1/v1/chat/completions"],
        key_env=[],
        tokens_per_minute=None,
        pricing={"input": 0, "output": 0, "cached_read": 0},
        variants={},
        withdrawn=False,
        reason=None,
    )
    session = RunSession(
        run_dir,
        job,
        card,
        Limits.from_dict({}, repetitions=1, verifier_timeout_multiplier=1.0),
        SimpleNamespace(host={"host_profile": {"memory_cap": "kernel"}}),
        {},
        ToolIdentity("1.0.0", "a" * 40),
    )
    session.start_services(restart=False)
    pids_path.write_text(
        json.dumps(
            {
                "proxy": session._proxy.pid,
                "sampler": session._sampler.pid,
                "memory_watch": session._memory_watch.pid,
            }
        ),
        encoding="utf-8",
    )
    while True:
        time.sleep(1)


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def test_service_process_groups_exit_when_runner_is_killed(tmp_path: Path) -> None:
    with socket.socket() as reserve:
        reserve.bind(("127.0.0.1", 0))
        port = int(reserve.getsockname()[1])
    run_dir = tmp_path / "run"
    pids_path = tmp_path / "service-pids.json"
    helper = subprocess.Popen(
        [
            sys.executable,
            str(Path(__file__).resolve()),
            "--service-helper",
            str(run_dir),
            str(port),
            str(pids_path),
        ],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    groups: list[int] = []
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and not pids_path.exists() and helper.poll() is None:
            time.sleep(0.05)
        if not pids_path.exists():
            output = helper.communicate(timeout=2)[0]
            raise AssertionError(f"service helper did not start: {output}")
        process_ids = json.loads(pids_path.read_text(encoding="utf-8"))
        groups = [process_ids["proxy"], process_ids["sampler"], process_ids["memory_watch"]]
        helper.send_signal(signal.SIGKILL)
        helper.communicate(timeout=2)

        deadline = time.monotonic() + 20
        alive = [pgid for pgid in groups if _group_alive(pgid)]
        while alive and time.monotonic() < deadline:
            time.sleep(0.1)
            alive = [pgid for pgid in groups if _group_alive(pgid)]

        assert not alive
    finally:
        if helper.poll() is None:
            helper.kill()
            helper.communicate(timeout=2)
        for pgid in groups:
            if _group_alive(pgid):
                with suppress(ProcessLookupError):
                    os.killpg(pgid, signal.SIGTERM)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and any(_group_alive(pgid) for pgid in groups):
            time.sleep(0.05)
        for pgid in groups:
            if _group_alive(pgid):
                with suppress(ProcessLookupError):
                    os.killpg(pgid, signal.SIGKILL)


if __name__ == "__main__" and len(sys.argv) > 1 and sys.argv[1] == "--service-helper":
    raise SystemExit(_service_helper(Path(sys.argv[2]), int(sys.argv[3]), Path(sys.argv[4])))
