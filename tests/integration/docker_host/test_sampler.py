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


def test_sampler_exits_when_lifeline_closes(tmp_path: Path) -> None:
    output = tmp_path / "resource-stats.jsonl"
    read_fd, write_fd = os.pipe()
    help_result = subprocess.run(
        [sys.executable, "-m", "harbor_research_runner.docker_host.sampler", "--help"],
        check=True,
        capture_output=True,
        text=True,
    )
    command = [
        sys.executable,
        "-m",
        "harbor_research_runner.docker_host.sampler",
        "--out",
        str(output),
        "--interval",
        "0.05",
    ]
    if "--lifeline-fd" in help_result.stdout:
        command.extend(["--lifeline-fd", str(read_fd)])
    process = subprocess.Popen(
        command,
        pass_fds=(read_fd,),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    os.close(read_fd)
    try:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not output.exists():
            time.sleep(0.01)
        assert output.exists()
        os.close(write_fd)
        write_fd = -1

        assert process.wait(timeout=3) == 0
    finally:
        if write_fd >= 0:
            os.close(write_fd)
        if process.poll() is None:
            process.send_signal(signal.SIGTERM)
            process.wait(timeout=2)
