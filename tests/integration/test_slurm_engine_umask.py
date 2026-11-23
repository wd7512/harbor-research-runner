from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path

ROOT = Path(__file__).parents[2]
ENGINE_SCRIPT = ROOT / "scripts/slurm/docker-engine.sh"


def test_rootless_runc_wrapper_sets_umask_and_is_used_by_engine(tmp_path: Path) -> None:
    work = tmp_path / "work"
    fake_bin = tmp_path / "bin"
    real_runc = work / "docker/runc"
    real_runc.parent.mkdir(parents=True)
    fake_bin.mkdir()
    real_runc.write_text(
        '#!/bin/sh\nprintf \'%s\\n\' "$(umask)" "$*"\n',
        encoding="utf-8",
    )
    real_runc.chmod(0o755)
    (fake_bin / "dockerd-rootless.sh").write_text(
        '#!/bin/bash\ncommand -v runc >"$ENGINE_RUNC_PATH"\nexec sleep 30\n',
        encoding="utf-8",
    )
    (fake_bin / "docker").write_text('#!/bin/sh\n[ "$1" = info ]\n', encoding="utf-8")
    (fake_bin / "dockerd-rootless.sh").chmod(0o755)
    (fake_bin / "docker").chmod(0o755)
    script = (
        "set -eu\n"
        f"work={shlex.quote(str(work))}\n"
        f"log={shlex.quote(str(tmp_path / 'engine.log'))}\n"
        f"source {shlex.quote(str(ENGINE_SCRIPT))}\n"
        "fetch_docker_release() { :; }\n"
        "ensure_compose_plugin() { :; }\n"
        "umask 000\n"
        "install_runc_umask_wrapper\n"
        "runc state x\n"
        "start_rootless_docker_engine\n"
        "set +e\n"
        "stop_docker_engine\n"
    )
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fake_bin}:{env['PATH']}",
            "HRR_STORAGE_DRIVER": "vfs",
            "ENGINE_RUNC_PATH": str(tmp_path / "daemon-runc-path"),
        }
    )

    result = subprocess.run(
        ["bash", "-c", script], text=True, capture_output=True, env=env, timeout=4, check=False
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == "0022\nstate x\n"
    assert (tmp_path / "daemon-runc-path").read_text(encoding="utf-8").strip() == str(
        work / "runc-umask/runc"
    )
