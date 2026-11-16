from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

from harbor_research_runner.docker_host.processes import pids_naming_run


def test_process_listing_uses_unlimited_width_ps_and_matches_command_paths(tmp_path: Path) -> None:
    commands: list[list[str]] = []
    run_dir = tmp_path / "runs" / "study with spaces"

    def run(argv: list[str], **_kwargs) -> SimpleNamespace:
        commands.append(argv)
        return SimpleNamespace(
            returncode=0,
            stdout=(
                f"  {os.getpid()} 1 pytest {run_dir}\n"
                f"  432 431 uv run harbor --jobs-dir {run_dir}\n"
                "  433 1 unrelated process\n"
            ),
        )

    assert pids_naming_run(run_dir, run=run) == [432]
    assert commands == [["ps", "-ww", "-axo", "pid=,ppid=,command="]]


def test_process_listing_skips_the_callers_own_ancestors(tmp_path: Path) -> None:
    run_dir = tmp_path / "runs" / "run-a"
    me = os.getpid()

    def run(_argv: list[str], **_kwargs) -> SimpleNamespace:
        return SimpleNamespace(
            returncode=0,
            stdout=(
                f"  900 1 zsh -c uv run hrr resume {run_dir}\n"
                f"  901 900 uv run hrr resume {run_dir}\n"
                f"  {me} 901 python -m hrr resume {run_dir}\n"
                f"  950 1 harbor_guard -- harbor jobs resume -p {run_dir}/harbor-job\n"
            ),
        )

    assert pids_naming_run(run_dir, run=run) == [950]
