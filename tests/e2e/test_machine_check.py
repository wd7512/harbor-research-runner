from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.timeout(300)
def test_check_machine_includes_the_bundled_say_hello_task(tmp_path: Path) -> None:
    if shutil.which("docker") is None:
        pytest.skip("docker not installed")
    docker = subprocess.run(["docker", "info"], capture_output=True, check=False)
    if docker.returncode != 0:
        pytest.skip("docker daemon is unavailable")
    containers = subprocess.run(
        ["docker", "ps", "-a", "--format", "{{.Names}}"], capture_output=True, text=True
    )
    networks = subprocess.run(
        ["docker", "network", "ls", "--format", "{{.Name}}"], capture_output=True, text=True
    )
    if containers.returncode != 0 or networks.returncode != 0:
        pytest.skip("Docker inventory is unavailable")
    if any(re.search(r"__env-main-\d+$", name) for name in containers.stdout.splitlines()) or any(
        re.search(r"__env_default$", name) for name in networks.stdout.splitlines()
    ):
        pytest.skip("Docker has trial residue; machine check requires a clean host")
    hrr = shutil.which("hrr")
    if hrr is None:
        pytest.skip("hrr is not installed")

    task_root = Path(__file__).parents[2] / "src" / "harbor_research_runner" / "run" / "canary_task"
    job_path = tmp_path / "job.yaml"
    job_path.write_text(
        "\n".join(
            [
                "name: say-hello-machine-check",
                "model: provider/model",
                "repetitions: 1",
                "concurrency: 1",
                "task_order: as_listed",
                "tasks:",
                f"  path: {task_root}",
                "  include: [say-hello]",
                "  exclude: []",
                "machine_check: machine-check.json",
                "arms:",
                "  - label: fake",
                "    harness: fake",
                f"    commit: {'a' * 40}",
                "proxy:",
                "  host: 127.0.0.1",
                "  port: 8766",
                "  container_host: host.docker.internal",
                "limits:",
                "  min_spare_memory_mb: 0",
                "  max_swapped_out_pages: 1000000",
                "  min_free_disk_gb: 0",
                "",
            ]
        ),
        encoding="utf-8",
    )
    output = tmp_path / "machine-check.json"

    result = subprocess.run(
        [hrr, "check-machine", "--job", str(job_path), "--out", str(output)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    check = json.loads(output.read_text(encoding="utf-8"))
    assert check["included"] == ["say-hello"]
