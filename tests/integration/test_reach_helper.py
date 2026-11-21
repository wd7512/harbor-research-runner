import functools
import http.server
import os
import shlex
import socket
import subprocess
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HELPER = ROOT / "scripts/slurm/reach-helper.sh"


DEAD_PROXY = "http://127.0.0.1:9"


def _port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _run(script: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", script],
        cwd=ROOT,
        env={**env, "http_proxy": DEAD_PROXY, "HTTP_PROXY": DEAD_PROXY},
        capture_output=True,
        text=True,
        timeout=4,
        check=False,
    )


@contextmanager
def _serving(root: Path) -> Iterator[int]:
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(root))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()


def test_start_reach_helper_serves_from_the_folder_without_directory_option(
    tmp_path: Path,
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    record = tmp_path / "python3-call"
    fake = bin_dir / "python3"
    fake.write_text(
        "#!/bin/sh\n"
        'for arg in "$@"; do\n'
        '  if [ "$arg" = --directory ]; then exit 2; fi\n'
        "done\n"
        f'{{ pwd; printf "%s\\n" "$@"; }} > {shlex.quote(str(record))}\n',
        encoding="utf-8",
    )
    fake.chmod(0o755)
    served = tmp_path / "served"
    served.mkdir()
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}
    result = _run(
        f'. "{HELPER}"; start_reach_helper "{served}" 41234; wait "$reach_helper_pid"',
        env,
    )
    assert result.returncode == 0, result.stderr
    cwd, *argv = record.read_text(encoding="utf-8").splitlines()
    assert Path(cwd).resolve() == served.resolve()
    assert argv == ["-m", "http.server", "41234", "--bind", "0.0.0.0"]


def test_reach_helper_up_reads_summary_through_a_proxy_environment(tmp_path: Path) -> None:
    (tmp_path / "summary.txt").write_text("reachable\n", encoding="utf-8")
    with _serving(tmp_path) as port:
        result = _run(f'. "{HELPER}"; reach_helper_up {port} 3', os.environ.copy())
    assert result.returncode == 0, result.stderr


def test_reach_helper_reports_down_when_port_refuses_connection() -> None:
    port = _port()
    result = _run(
        f'. "{HELPER}"; ! reach_helper_up {port}',
        os.environ.copy(),
    )
    assert result.returncode == 0, result.stderr
