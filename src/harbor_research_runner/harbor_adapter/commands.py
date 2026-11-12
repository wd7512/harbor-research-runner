from pathlib import Path

import yaml

from harbor_research_runner.job_config.job_file import Job

NEUTRAL_RESUME_FILTER = "hrr-erases-nothing"
PLUGIN = "harbor_research_runner.harbor_adapter.plugin:RunPlugin"


def job_config(
    job: Job,
    task_names: list[str],
    agents: list[tuple[str, dict]],
    verifier_timeout_multiplier: float,
    job_name: str = "harbor-job",
) -> dict:
    config = {
        "job_name": job_name,
        "n_attempts": job.repetitions,
        "n_concurrent_trials": job.concurrency,
        "retry": {"max_retries": 0},
        "verifier_timeout_multiplier": verifier_timeout_multiplier,
        "agents": [
            {"import_path": import_path, "kwargs": settings} for import_path, settings in agents
        ],
    }
    config.update(_task_entries(job, task_names))
    return config


def _task_entries(job: Job, task_names: list[str]) -> dict:
    if job.tasks.kind == "dataset":
        return {"datasets": [{"name": job.tasks.dataset, "task_names": task_names}]}
    if job.tasks.kind == "path":
        return {"datasets": [{"path": job.tasks.path, "task_names": task_names}]}
    return {
        "tasks": [
            {
                "path": task_name,
                "git_url": job.tasks.git_url,
                "git_commit_id": job.tasks.git_commit,
            }
            for task_name in task_names
        ]
    }


def oracle_job_config(job: Job, task_names: list[str], agent_name: str, job_name: str) -> dict:
    if agent_name not in {"oracle", "nop"}:
        raise ValueError(f"unsupported machine-check agent {agent_name!r}")
    return {
        "job_name": job_name,
        "n_attempts": 1,
        "n_concurrent_trials": job.concurrency,
        "retry": {"max_retries": 0},
        "verifier_timeout_multiplier": 1.0,
        "agents": [{"name": agent_name}],
        **_task_entries(job, task_names),
    }


def machine_check_command(check_dir: Path, job_name: str, config_name: str) -> list[str]:
    return [
        "uv",
        "run",
        "harbor",
        "run",
        "--config",
        str(check_dir / config_name),
        "--jobs-dir",
        str(check_dir),
        "--job-name",
        job_name,
    ]


def write_job_config(path: Path, config: dict) -> None:
    content = yaml.safe_dump(config, sort_keys=True)
    if path.exists():
        if path.read_text(encoding="utf-8") != content:
            raise FileExistsError(f"job config already exists with different content: {path}")
        return
    path.write_text(content, encoding="utf-8")


def run_command(run_dir: Path, job_name: str, proxy_url: str, task_order: str) -> list[str]:
    return [
        "uv",
        "run",
        "harbor",
        "run",
        "--config",
        str(run_dir / "harbor-job-config.yaml"),
        "--jobs-dir",
        str(run_dir),
        "--job-name",
        job_name,
        "--plugin",
        PLUGIN,
        "--pk",
        f"proxy_url={proxy_url}",
        "--pk",
        f"limits_file={run_dir / 'proxy-config.json'}",
        "--pk",
        f"task_order={task_order}",
    ]


def resume_command(run_dir: Path, job_name: str, proxy_url: str, task_order: str) -> list[str]:
    return [
        "uv",
        "run",
        "harbor",
        "jobs",
        "resume",
        "-p",
        str(run_dir / job_name),
        "-f",
        NEUTRAL_RESUME_FILTER,
        "--plugin",
        PLUGIN,
        "--pk",
        f"proxy_url={proxy_url}",
        "--pk",
        f"limits_file={run_dir / 'proxy-config.json'}",
        "--pk",
        f"task_order={task_order}",
    ]
