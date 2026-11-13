import json
import shutil
from dataclasses import replace
from pathlib import Path

import pytest
from harbor.models.task import id as harbor_task_id

from harbor_research_runner.docker_host.cleanup import Inventory
from harbor_research_runner.docker_host.host_lock import HostLock
from harbor_research_runner.job_config.job_file import Arm, Job, TaskSource
from harbor_research_runner.records.machine_check import read
from harbor_research_runner.records.resource_stats import Sample
from harbor_research_runner.run.checks import CheckDeps
from harbor_research_runner.run.machine_check import (
    MachineCheckDeps,
    included_tasks,
    run_machine_check,
    task_arches,
    verifier_multiplier,
)

FIXTURE_ROOT = Path(__file__).parents[2] / "fixtures" / "harbor_job"
TASK = "terminal-bench/bn-fit-modify"
TASK_DIGEST = "b5f9644970c17ad9ddb46b7266f7bcd87c761d77d7e6f55d7cfe7284d5ff66e9"


def _job() -> Job:
    return Job(
        path=Path("job.yaml"),
        name="machine-check",
        model="provider/model",
        repetitions=1,
        concurrency=1,
        task_order="as_listed",
        tasks=TaskSource(
            "dataset", "terminal-bench/terminal-bench-2-1", None, None, None, (TASK,), ()
        ),
        machine_check=Path("check.json"),
        arms=(Arm("arm-a", "fake", "a" * 40),),
        proxy_host="127.0.0.1",
        proxy_port=8766,
        container_host="host.docker.internal",
        limit_overrides={},
    )


def _checks(tmp_path: Path) -> CheckDeps:
    return CheckDeps(
        select_adapter=lambda *_: object(),
        measure=lambda: Sample(1.0, 4096, 0, 100.0, 0.0, 0, True),
        inventory=lambda: Inventory(True, "ok", (), (), (), ()),
        live_run=lambda _path: None,
        host_lock=HostLock(tmp_path / "host.lock"),
        read_machine_check=lambda _path: None,
        host_fingerprint=lambda: "fingerprint",
        harbor_version=lambda: "0.23.0",
        bind_port=lambda _host, _port: None,
    )


def _fake_harbor(tmp_path: Path, host_lock: HostLock):
    def run(argv: list[str], cwd: Path) -> int:
        assert host_lock.held
        job_name = argv[argv.index("--job-name") + 1]
        trial_dir = cwd / job_name / "pass-bn-fit"
        shutil.copytree(FIXTURE_ROOT / "pass-bn-fit", trial_dir)
        config = json.loads((trial_dir / "config.json").read_text(encoding="utf-8"))
        agent = "nop" if job_name == "nop-job" else "oracle"
        config["agent"] = {"name": agent}
        (trial_dir / "config.json").write_text(json.dumps(config), encoding="utf-8")
        if agent == "nop":
            result_path = trial_dir / "result.json"
            result = json.loads(result_path.read_text(encoding="utf-8"))
            result["verifier_result"]["rewards"]["reward"] = 0.0
            result_path.write_text(json.dumps(result), encoding="utf-8")
        return 0

    return run


@pytest.mark.parametrize(
    ("verifier_s", "expected"),
    [(0.2, 1.0), (0.5, 1.0), (0.9, 2.0), (1.4, 3.0)],
)
def test_verifier_multiplier_rounds_up_half_steps_over_included_tasks(
    verifier_s: float, expected: float
) -> None:
    assert (
        verifier_multiplier(
            [
                {
                    "name": "included",
                    "oracle_reward": 1.0,
                    "nothing_reward": 0.0,
                    "verifier_s": verifier_s,
                    "verifier_budget_s": 1.0,
                },
            ]
        )
        == expected
    )


def test_included_tasks_require_oracle_pass_and_do_nothing_failure() -> None:
    tasks = [
        {"name": "oracle-failed", "oracle_reward": 0.0, "nothing_reward": 0.0},
        {"name": "nop-succeeded", "oracle_reward": 1.0, "nothing_reward": 1.0},
        {"name": "included", "oracle_reward": 1.0, "nothing_reward": 0.0},
        {"name": "no-nop-check", "oracle_reward": 1.0, "nothing_reward": None},
    ]

    assert included_tasks(tasks) == ["included", "no-nop-check"]


def test_task_arches_inspects_declared_local_docker_image() -> None:
    task_tomls = {
        "terminal-bench/fix-git": (
            '[task]\nname = "terminal-bench/fix-git"\n'
            '[environment]\ndocker_image = "alexgshaw/fix-git:20260403"\n'
        )
    }
    digests = {"terminal-bench/fix-git": "16948b98"}

    arches = task_arches(
        task_tomls,
        digests,
        "arm64",
        image_arch=lambda image: "amd64" if image == "alexgshaw/fix-git:20260403" else "arm64",
    )

    assert arches == {"terminal-bench/fix-git": "amd64"}


def test_task_arches_uses_host_architecture_for_dockerfile_tasks() -> None:
    task_tomls = {"local-task": '[task]\nname = "local-task"\n[environment]\n'}
    digests = {"local-task": "sha256:task"}

    arches = task_arches(
        task_tomls,
        digests,
        "arm64",
        image_arch=lambda _image: pytest.fail("Dockerfile tasks use the host architecture"),
    )

    assert arches == {"local-task": "arm64"}


def test_task_arches_names_task_when_declared_image_is_unavailable() -> None:
    task_tomls = {
        "task-a": '[task]\nname = "task-a"\n[environment]\ndocker_image = "missing/image"\n'
    }

    with pytest.raises(ValueError, match=r"task-a.*missing/image"):
        task_arches(
            task_tomls,
            {"task-a": "sha256:task"},
            "arm64",
            image_arch=lambda _image: (_ for _ in ()).throw(ValueError("not local")),
        )


def test_run_machine_check_writes_results_and_keeps_the_host_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package_cache = tmp_path / "cache" / "tasks" / "packages"
    task_dir = package_cache / "terminal-bench" / "bn-fit-modify" / TASK_DIGEST
    task_dir.mkdir(parents=True)
    shutil.copyfile(FIXTURE_ROOT / "task-tomls" / "bn-fit-modify.task.toml", task_dir / "task.toml")
    monkeypatch.setattr(harbor_task_id, "PACKAGE_CACHE_DIR", package_cache)
    checks = _checks(tmp_path)
    deps = MachineCheckDeps(
        checks,
        _fake_harbor(tmp_path, checks.host_lock),
        lambda: 1_800_000_000.0,
        host_arch=lambda: "arm64",
    )
    out = tmp_path / "checks" / "machine-check.json"

    result = run_machine_check(_job(), out, True, tmp_path / "runs", deps)

    assert read(out) == result
    assert result.included == [TASK]
    assert result.tasks[0]["oracle_reward"] == 1.0
    assert result.tasks[0]["arch"] == "arm64"
    assert result.tasks[0]["agent_budget_s"] == 3600.0
    assert result.tasks[0]["nothing_reward"] == 0.0
    assert result.host_fingerprint == "fingerprint"
    assert checks.host_lock.held
    checks.host_lock.release()


def test_run_machine_check_uses_local_task_id_for_all_task_rows(
    tmp_path: Path,
) -> None:
    task_dir = tmp_path / "say-hello"
    task_dir.mkdir()
    (task_dir / "task.toml").write_text(
        '[task]\nname = "org/say-hello"\n[agent]\ntimeout_sec = 12\n'
        "[verifier]\ntimeout_sec = 3\n[environment]\n"
        'docker_image = "example/task:latest"\n',
        encoding="utf-8",
    )
    digest = "sha256:" + "a" * 64

    def local_harbor(argv: list[str], cwd: Path) -> int:
        job_name = argv[argv.index("--job-name") + 1]
        trial_dir = cwd / job_name / "say-hello__trial"
        shutil.copytree(FIXTURE_ROOT / "pass-bn-fit", trial_dir)
        config = json.loads((trial_dir / "config.json").read_text(encoding="utf-8"))
        config["task"] = {"path": str(task_dir)}
        config["agent"] = {"name": "oracle" if job_name == "harbor-job" else "nop"}
        (trial_dir / "config.json").write_text(json.dumps(config), encoding="utf-8")
        lock = json.loads((trial_dir / "lock.json").read_text(encoding="utf-8"))
        lock["task"] = {"name": "say-hello", "digest": digest}
        (trial_dir / "lock.json").write_text(json.dumps(lock), encoding="utf-8")
        result_path = trial_dir / "result.json"
        result = json.loads(result_path.read_text(encoding="utf-8"))
        result["task_name"] = "org/say-hello"
        if job_name == "nop-job":
            result["verifier_result"]["rewards"]["reward"] = 0.0
        result_path.write_text(json.dumps(result), encoding="utf-8")
        return 0

    checks = _checks(tmp_path)
    deps = MachineCheckDeps(
        checks,
        local_harbor,
        lambda: 1_800_000_000.0,
        host_arch=lambda: "arm64",
        image_arch=lambda image: "amd64" if image == "example/task:latest" else "arm64",
    )
    job = replace(
        _job(),
        tasks=TaskSource("path", None, str(task_dir), None, None, ("say-hello",), ()),
    )

    result = run_machine_check(
        job, tmp_path / "checks" / "local.json", True, tmp_path / "runs", deps
    )

    assert result.included == ["say-hello"]
    task = result.tasks[0]
    assert task["name"] == "say-hello"
    assert task["digest"] == digest
    assert task["arch"] == "amd64"
    assert task["oracle_reward"] == 1.0
    assert task["agent_budget_s"] == 12.0
    assert task["verifier_budget_s"] == 3.0
    assert task["nothing_reward"] == 0.0
    assert task["agent_s"] > 0
    assert task["verifier_s"] > 0
    checks.host_lock.release()
