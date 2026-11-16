from __future__ import annotations

import os
import re
import shlex
import signal
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def _commands(page: Path) -> list[list[str]]:
    text = page.read_text(encoding="utf-8")
    blocks = re.findall(r"```sh\s*\n(.*?)\n\s*```", text, re.DOTALL)
    return [shlex.split(line) for block in blocks for line in block.splitlines() if line.strip()]


def _command(commands: list[list[str]], name: str) -> list[str]:
    return next(
        command
        for command in commands
        if command[:3] == ["uv", "run", "hrr"] and name in command[3:]
    )


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _run(command: list[str], cwd: Path, timeout: int = 900) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def test_how_to_commands_run_from_the_study_folder(e2e_run) -> None:
    """Skip build-harness because OpenCode image builds are out of scope; step 56 covers --resume."""
    page1 = _commands(ROOT / "docs/how-to/how-to-run-a-study.md")
    page3 = _commands(ROOT / "docs/how-to/how-to-fix-or-exclude-a-task.md")
    study_root = e2e_run.root
    study_root.joinpath("checks").mkdir()
    e2e_run.job_data["machine_check"] = "checks/machine-check.json"
    e2e_run.machine_check_path = study_root / "checks/machine-check.json"
    e2e_run.job_path = study_root / "study.yaml"
    e2e_run.job_path.write_text(yaml.safe_dump(e2e_run.job_data, sort_keys=True), encoding="utf-8")

    check_machine = _command(page1, "check-machine")
    check_with_nop = next(command for command in page3 if "--with-do-nothing" in command)
    run_study = next(
        command
        for command in page1
        if command[:4] == ["uv", "run", "hrr", "run"] and "study.yaml" in command
    )
    build_evidence = _command(page1, "build-evidence")
    explore = _command(page1, "explore")

    for command in (check_machine, check_with_nop):
        result = _run(command, study_root)
        assert result.returncode == 0, result.stdout + result.stderr
    assert e2e_run.machine_check_path.is_file()

    result = _run(run_study, study_root)
    assert result.returncode == 0, result.stdout + result.stderr
    run_dirs = [
        path
        for path in (study_root / "runs").iterdir()
        if path.is_dir() and (path / "run-metadata.json").is_file()
    ]
    assert len(run_dirs) == 1
    run_id = run_dirs[0].name
    assert (study_root / "evidence" / run_id / "manifest.json").is_file()

    evidence_command = [*build_evidence[:-1], run_id]
    result = _run(evidence_command, study_root)
    assert result.returncode == 0, result.stdout + result.stderr

    port = _free_port()
    explore_command = [*explore, "--port", str(port)]
    server = subprocess.Popen(
        explore_command,
        cwd=study_root,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if server.poll() is not None:
                output, _ = server.communicate()
                raise AssertionError(f"explore exited before serving: {output}")
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=1) as response:
                    assert response.status == 200
                    break
            except (OSError, urllib.error.URLError):
                time.sleep(0.1)
        else:
            raise AssertionError("explore did not serve evidence")
    finally:
        if server.poll() is None:
            os.killpg(server.pid, signal.SIGTERM)
        try:
            server.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(server.pid, signal.SIGKILL)
            server.communicate(timeout=5)
