from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/slurm/run-study.sbatch"


def _run(
    tmp_path: Path, command: str | None, hrr_exit: int = 0
) -> tuple[subprocess.CompletedProcess[str], Path, Path]:
    study = tmp_path / "study"
    study.mkdir()
    if command == "check-machine":
        (study / ".hrr-run-id").write_text("existing-run\n", encoding="utf-8")
    env_file = tmp_path / "model.env"
    env_file.write_text("MODEL_KEY=test\n", encoding="utf-8")
    env_file.chmod(0o600)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "calls.log"
    hrr = bin_dir / "hrr"
    hrr.write_text(
        "#!/bin/bash\n"
        f"printf '%s\\n' \"$*\" >> '{log}'\n"
        f'if [ "$1" = check-machine ] || [ "$1" = run ]; then exit {hrr_exit}; fi\n'
        "exit 0\n",
        encoding="utf-8",
    )
    hrr.chmod(0o700)
    slurm_dir = tmp_path / "slurm"
    slurm_dir.mkdir()
    (slurm_dir / "docker-engine.sh").write_text(
        "engine_selected=test\n"
        "start_docker_engine() { printf 'engine-start\\n' >> \"$CALL_LOG\"; return 0; }\n"
        "stop_docker_engine() { printf 'engine-stop\\n' >> \"$CALL_LOG\"; }\n",
        encoding="utf-8",
    )
    script = tmp_path / "run-study.sbatch"
    script.write_text(
        SCRIPT.read_text(encoding="utf-8").replace(
            'work="/tmp/hrr-study-$job_id"', 'work="$HRR_TEST_WORK/hrr-study-$job_id"'
        ),
        encoding="utf-8",
    )
    environment = os.environ.copy()
    environment.update(
        {
            "PATH": f"{bin_dir}:{environment['PATH']}",
            "HRR_STUDY_DIR": str(study),
            "HRR_REF": "v0.1.1",
            "HRR_ENV_FILE": str(env_file),
            "HRR_SLURM_DIR": str(slurm_dir),
            "HRR_TEST_WORK": str(tmp_path / "work"),
            "SLURM_JOB_ID": "123",
            "SLURM_JOB_END_TIME": "4102444800",
            "CALL_LOG": str(log),
            "UV_TOOL_DIR": str(tmp_path / "uv-tools"),
            "UV_TOOL_BIN_DIR": str(bin_dir),
        }
    )
    if command is not None:
        environment["HRR_COMMAND"] = command
    else:
        environment.pop("HRR_COMMAND", None)
    (bin_dir / "uv").write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")
    (bin_dir / "uv").chmod(0o700)
    (bin_dir / "stat").write_text("#!/bin/bash\nprintf '600\\n'\n", encoding="utf-8")
    (bin_dir / "stat").chmod(0o700)
    (bin_dir / "date").write_text(
        "#!/bin/bash\n"
        "if [ \"$1\" = -d ]; then printf '2029-12-31T00:00:00+00:00\\n'; "
        "else printf '1000000000\\n'; fi\n",
        encoding="utf-8",
    )
    (bin_dir / "date").chmod(0o700)
    result = subprocess.run(
        ["bash", str(script)],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=4,
        check=False,
    )
    return result, log, study


def test_check_machine_runs_and_propagates_hrr_exit_code(tmp_path: Path) -> None:
    result, log, study = _run(tmp_path, "check-machine", hrr_exit=3)

    assert result.returncode == 3
    calls = log.read_text(encoding="utf-8").splitlines()
    assert (
        "check-machine --job study.yaml --out checks/machine-check.json --with-do-nothing" in calls
    )
    assert "machine check exited 3; see checks/machine-check.json" in result.stdout
    assert (study / "checks").is_dir()
    assert (study / ".hrr-run-id").read_text(encoding="utf-8") == "existing-run\n"


def test_default_still_runs_the_study(tmp_path: Path) -> None:
    result, log, _ = _run(tmp_path, None)

    assert result.returncode == 0
    calls = log.read_text(encoding="utf-8").splitlines()
    run_call = next(call for call in calls if call.startswith("run study.yaml"))
    assert run_call.startswith("run study.yaml --runs-dir ")
    assert "--evidence-dir evidence" in run_call
    assert "--stop-launching-at" in run_call


def test_unknown_command_is_rejected_before_engine_or_hrr(tmp_path: Path) -> None:
    result, log, _ = _run(tmp_path, "other")

    assert result.returncode == 64
    assert "refusing: unsupported HRR_COMMAND 'other'" in result.stderr
    assert not log.exists()
