import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from harbor_research_runner.records.jsonl import read_rows

TWO_EVENTS_IN_ONE_WRITE = (
    '#!/bin/sh\nprintf \'{"Action":"oom","n":1}\\n{"Action":"oom","n":2}\\n\'\nexec sleep 30\n'
)


def _stub_docker_env(tmp_path: Path, script: str) -> dict[str, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text(script, encoding="utf-8")
    docker.chmod(0o755)
    return {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"}


def _start(tmp_path: Path, output: Path, script: str) -> subprocess.Popen:
    return subprocess.Popen(
        [
            sys.executable,
            "-m",
            "harbor_research_runner.docker_host.oom_watch",
            "--out",
            str(output),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=_stub_docker_env(tmp_path, script),
    )


def test_oom_watch_records_every_event_of_a_burst_while_the_stream_stays_open(
    tmp_path: Path,
) -> None:
    output = tmp_path / "docker-oom-events.jsonl"
    process = _start(tmp_path, output, TWO_EVENTS_IN_ONE_WRITE)
    try:
        deadline = time.monotonic() + 3
        rows: list[dict] = []
        while time.monotonic() < deadline and len(rows) < 2:
            time.sleep(0.1)
            if output.exists():
                rows = read_rows(output).rows
        assert [row["n"] for row in rows] == [1, 2]
        assert process.poll() is None
    finally:
        process.send_signal(signal.SIGTERM)
        process.wait(timeout=5)


def test_oom_watch_exits_nonzero_when_the_docker_stream_ends(tmp_path: Path) -> None:
    output = tmp_path / "docker-oom-events.jsonl"
    process = _start(tmp_path, output, "#!/bin/sh\nexit 0\n")

    assert process.wait(timeout=4) != 0
