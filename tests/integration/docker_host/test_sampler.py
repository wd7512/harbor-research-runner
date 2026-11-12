import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path


def test_sampler_writes_complete_rows_until_sigterm(tmp_path: Path) -> None:
    output = tmp_path / "resource-stats.jsonl"
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "harbor_research_runner.docker_host.sampler",
            "--out",
            str(output),
            "--interval",
            "0.2",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 4
        rows = []
        while time.monotonic() < deadline:
            if output.exists():
                content = output.read_text(encoding="utf-8")
                rows = [json.loads(line) for line in content.splitlines()]
                if len(rows) >= 3:
                    break
            time.sleep(0.02)
        assert len(rows) >= 3
        os.kill(process.pid, signal.SIGTERM)
        assert process.wait(timeout=2) == 0
    finally:
        if process.poll() is None:
            os.kill(process.pid, signal.SIGTERM)
            process.wait(timeout=2)

    content = output.read_bytes()
    assert content.endswith(b"\n")
    assert all(json.loads(line)["type"] == "sample" for line in content.splitlines())
