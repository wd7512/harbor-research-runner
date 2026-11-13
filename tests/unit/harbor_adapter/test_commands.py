from pathlib import Path

import pytest
import yaml
from harbor.models.job.config import JobConfig

from harbor_research_runner.harbor_adapter.commands import (
    apply_memory_policy,
    job_config,
    machine_check_command,
    oracle_job_config,
    resume_command,
    run_command,
    write_job_config,
)
from harbor_research_runner.job_config.job_file import Arm, Job, TaskSource

AGENT_SETTINGS = (
    {"arm": "arm-a", "nested": {"requests": 3}, "flag": True},
    {"arm": "arm-b", "prompt": "line one\nline two"},
)


def make_job(tasks: TaskSource) -> Job:
    return Job(
        path=Path("job.yaml"),
        name="study",
        model="provider/model",
        repetitions=3,
        concurrency=4,
        task_order="as_listed",
        tasks=tasks,
        machine_check=Path("check.json"),
        arms=(Arm("arm-a", "opencode", "a" * 40), Arm("arm-b", "opencode", "b" * 40)),
        proxy_host="127.0.0.1",
        proxy_port=8766,
        container_host="host.docker.internal",
        limit_overrides={},
    )


def agents() -> list[tuple[str, dict]]:
    return [("package.Agent", settings) for settings in AGENT_SETTINGS]


def expected_agents() -> list[dict]:
    return [{"import_path": "package.Agent", "kwargs": settings} for settings in AGENT_SETTINGS]


def test_job_config_for_registry_dataset_matches_harbor_config():
    job = make_job(TaskSource("dataset", "org/dataset", None, None, None, (), ()))

    config = job_config(job, ["task-a", "task-b", "task-c"], agents(), 1.5)

    assert config == {
        "job_name": "harbor-job",
        "n_attempts": 3,
        "n_concurrent_trials": 4,
        "retry": {"max_retries": 0},
        "verifier_timeout_multiplier": 1.5,
        "agents": expected_agents(),
        "datasets": [{"name": "org/dataset", "task_names": ["task-a", "task-b", "task-c"]}],
    }
    parsed = JobConfig.model_validate(config)
    assert parsed.job_name == "harbor-job"
    assert parsed.n_attempts == 3
    assert parsed.n_concurrent_trials == 4
    assert parsed.retry.max_retries == 0
    assert parsed.verifier_timeout_multiplier == 1.5
    assert [agent.kwargs for agent in parsed.agents] == list(AGENT_SETTINGS)
    assert parsed.datasets[0].name == "org/dataset"
    assert parsed.datasets[0].task_names == ["task-a", "task-b", "task-c"]


def test_job_config_for_local_dataset_path_matches_harbor_config():
    job = make_job(TaskSource("path", None, "../tb21-fixed", None, None, (), ()))

    config = job_config(job, ["task-a", "task-b", "task-c"], agents(), 1.5)

    assert config == {
        "job_name": "harbor-job",
        "n_attempts": 3,
        "n_concurrent_trials": 4,
        "retry": {"max_retries": 0},
        "verifier_timeout_multiplier": 1.5,
        "agents": expected_agents(),
        "datasets": [{"path": "../tb21-fixed", "task_names": ["task-a", "task-b", "task-c"]}],
    }
    parsed = JobConfig.model_validate(config)
    assert parsed.job_name == "harbor-job"
    assert parsed.n_attempts == 3
    assert parsed.n_concurrent_trials == 4
    assert parsed.retry.max_retries == 0
    assert parsed.verifier_timeout_multiplier == 1.5
    assert [agent.kwargs for agent in parsed.agents] == list(AGENT_SETTINGS)
    assert parsed.datasets[0].path == Path("../tb21-fixed")
    assert parsed.datasets[0].task_names == ["task-a", "task-b", "task-c"]


def test_job_config_for_pinned_git_task_paths_matches_harbor_config():
    commit = "c" * 40
    job = make_job(TaskSource("git", None, None, "https://example.test/tasks.git", commit, (), ()))

    config = job_config(job, ["task-a", "task-b", "task-c"], agents(), 1.5)

    assert config == {
        "job_name": "harbor-job",
        "n_attempts": 3,
        "n_concurrent_trials": 4,
        "retry": {"max_retries": 0},
        "verifier_timeout_multiplier": 1.5,
        "agents": expected_agents(),
        "tasks": [
            {
                "path": task_name,
                "git_url": "https://example.test/tasks.git",
                "git_commit_id": commit,
            }
            for task_name in ["task-a", "task-b", "task-c"]
        ],
    }
    parsed = JobConfig.model_validate(config)
    assert parsed.job_name == "harbor-job"
    assert parsed.n_attempts == 3
    assert parsed.n_concurrent_trials == 4
    assert parsed.retry.max_retries == 0
    assert parsed.verifier_timeout_multiplier == 1.5
    assert [agent.kwargs for agent in parsed.agents] == list(AGENT_SETTINGS)
    assert [(task.path, task.git_url, task.git_commit_id) for task in parsed.tasks] == [
        (Path(task_name), "https://example.test/tasks.git", commit)
        for task_name in ["task-a", "task-b", "task-c"]
    ]


def test_job_config_uses_the_given_job_name():
    job = make_job(TaskSource("dataset", "org/dataset", None, None, None, (), ()))

    config = job_config(job, ["task-a"], agents(), 2.0, job_name="custom-job")

    assert config["job_name"] == "custom-job"
    assert JobConfig.model_validate(config).job_name == "custom-job"


def test_memory_policy_is_ignored_only_when_host_has_no_memory_cap():
    config = {"job_name": "harbor-job"}

    assert apply_memory_policy(config, "none") == {
        "job_name": "harbor-job",
        "memory_enforcement_policy": "ignore",
        "cpu_enforcement_policy": "ignore",
    }


def test_kernel_memory_policy_leaves_harbor_defaults_untouched():
    config = {"job_name": "harbor-job"}

    assert apply_memory_policy(config, "kernel") == {"job_name": "harbor-job"}


def test_write_job_config_writes_yaml_with_sorted_keys(tmp_path: Path):
    path = tmp_path / "harbor-job-config.yaml"
    config = {"job_name": "harbor-job", "zeta": 1, "alpha": {"y": 2, "b": 3}}

    write_job_config(path, config)

    assert path.read_text(encoding="utf-8") == yaml.safe_dump(config, sort_keys=True)


def test_write_job_config_is_idempotent_for_same_content(tmp_path: Path):
    path = tmp_path / "harbor-job-config.yaml"
    config = {"job_name": "harbor-job", "n_attempts": 3, "nested": {"a": 1}}
    write_job_config(path, config)
    original = path.read_bytes()

    write_job_config(path, config)

    assert path.read_bytes() == original


def test_write_job_config_refuses_different_content_and_preserves_bytes(tmp_path: Path):
    path = tmp_path / "harbor-job-config.yaml"
    write_job_config(path, {"job_name": "harbor-job"})
    original = path.read_bytes()

    with pytest.raises(FileExistsError):
        write_job_config(path, {"job_name": "changed"})

    assert path.read_bytes() == original


def test_run_command_matches_harbor_command_vector(tmp_path: Path):
    run_dir = tmp_path / "runs" / "study"

    command = run_command(run_dir, "harbor-job", "http://127.0.0.1:8766", "slowest_first")

    assert command == [
        "uv",
        "run",
        "harbor",
        "run",
        "--config",
        str(run_dir / "harbor-job-config.yaml"),
        "--jobs-dir",
        str(run_dir),
        "--job-name",
        "harbor-job",
        "--plugin",
        "harbor_research_runner.harbor_adapter.plugin:RunPlugin",
        "--pk",
        "proxy_url=http://127.0.0.1:8766",
        "--pk",
        f"limits_file={run_dir / 'proxy-config.json'}",
        "--pk",
        "task_order=slowest_first",
    ]


def test_run_command_keeps_custom_job_name_and_paths_with_spaces(tmp_path: Path):
    run_dir = tmp_path / "run with spaces" / "study"

    command = run_command(run_dir, "custom-job", "http://127.0.0.1:8766", "as_listed")

    assert command == [
        "uv",
        "run",
        "harbor",
        "run",
        "--config",
        str(run_dir / "custom-job-config.yaml"),
        "--jobs-dir",
        str(run_dir),
        "--job-name",
        "custom-job",
        "--plugin",
        "harbor_research_runner.harbor_adapter.plugin:RunPlugin",
        "--pk",
        "proxy_url=http://127.0.0.1:8766",
        "--pk",
        f"limits_file={run_dir / 'proxy-config.json'}",
        "--pk",
        "task_order=as_listed",
    ]


def test_oracle_job_config_uses_harbor_builtin_agent_name():
    job = make_job(TaskSource("dataset", "org/dataset", None, None, None, (), ()))

    config = oracle_job_config(job, ["task-a"], "oracle", "harbor-job")

    assert config == {
        "job_name": "harbor-job",
        "n_attempts": 1,
        "n_concurrent_trials": 4,
        "retry": {"max_retries": 0},
        "verifier_timeout_multiplier": 1.0,
        "agents": [{"name": "oracle"}],
        "datasets": [{"name": "org/dataset", "task_names": ["task-a"]}],
    }
    assert JobConfig.model_validate(config).agents[0].name == "oracle"


def test_oracle_job_config_rejects_unknown_agent_name():
    job = make_job(TaskSource("dataset", "org/dataset", None, None, None, (), ()))

    with pytest.raises(ValueError):
        oracle_job_config(job, ["task-a"], "other", "harbor-job")


def test_machine_check_command_has_no_plugin_or_proxy_settings(tmp_path: Path):
    check_dir = tmp_path / "check with spaces"

    command = machine_check_command(check_dir, "oracle", "oracle.yaml")

    assert command == [
        "uv",
        "run",
        "harbor",
        "run",
        "--config",
        str(check_dir / "oracle.yaml"),
        "--jobs-dir",
        str(check_dir),
        "--job-name",
        "oracle",
    ]


def test_resume_command_matches_harbor_command_vector(tmp_path: Path):
    run_dir = tmp_path / "runs" / "study"

    command = resume_command(run_dir, "harbor-job", "http://127.0.0.1:8766", "slowest_first")

    assert command == [
        "uv",
        "run",
        "harbor",
        "jobs",
        "resume",
        "-p",
        str(run_dir / "harbor-job"),
        "-f",
        "hrr-erases-nothing",
        "--plugin",
        "harbor_research_runner.harbor_adapter.plugin:RunPlugin",
        "--pk",
        "proxy_url=http://127.0.0.1:8766",
        "--pk",
        f"limits_file={run_dir / 'proxy-config.json'}",
        "--pk",
        "task_order=slowest_first",
    ]


def test_resume_command_keeps_custom_job_name_and_paths_with_spaces(tmp_path: Path):
    run_dir = tmp_path / "run with spaces" / "study"

    command = resume_command(run_dir, "custom-job", "http://127.0.0.1:8766", "as_listed")

    assert command == [
        "uv",
        "run",
        "harbor",
        "jobs",
        "resume",
        "-p",
        str(run_dir / "custom-job"),
        "-f",
        "hrr-erases-nothing",
        "--plugin",
        "harbor_research_runner.harbor_adapter.plugin:RunPlugin",
        "--pk",
        "proxy_url=http://127.0.0.1:8766",
        "--pk",
        f"limits_file={run_dir / 'proxy-config.json'}",
        "--pk",
        "task_order=as_listed",
    ]
