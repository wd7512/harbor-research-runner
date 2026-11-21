import os
import shlex
import shutil
import socket
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HELPER = ROOT / "scripts/slurm/reach-helper.sh"


def _port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _run(script: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", script],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=4,
        check=False,
    )


def _assert_reachable(root: Path, port: int, env: dict[str, str]) -> None:
    result = _run(
        f'. "{HELPER}"; start_reach_helper "{root}" {port}; '
        "trap 'kill \"$reach_helper_pid\" 2>/dev/null || true' EXIT; "
        'kill -0 "$reach_helper_pid" && '
        "for attempt in {1..10}; do reach_helper_up "
        + str(port)
        + " && exit 0; sleep 0.1; done; exit 1",
        env,
    )
    assert result.returncode == 0, result.stderr


def test_reach_helper_works_when_python_rejects_directory_option(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    real_python = shutil.which("python3")
    assert real_python is not None
    wrapper = bin_dir / "python3"
    wrapper.write_text(
        "#!/bin/sh\n"
        'for arg in "$@"; do\n'
        '  if [ "$arg" = --directory ]; then\n'
        "    echo 'error: unrecognized arguments: --directory' >&2\n"
        "    exit 2\n"
        "  fi\n"
        "done\n"
        f'exec {shlex.quote(real_python)} "$@"\n',
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    root = tmp_path / "served"
    root.mkdir()
    (root / "summary.txt").write_text("reachable\n", encoding="utf-8")
    port = _port()
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}
    _assert_reachable(root, port, env)


def test_reach_helper_serves_file_from_directory(tmp_path: Path) -> None:
    root = tmp_path / "served"
    root.mkdir()
    (root / "summary.txt").write_text("reachable\n", encoding="utf-8")
    _assert_reachable(root, _port(), os.environ.copy())


def test_reach_helper_reports_down_when_port_refuses_connection() -> None:
    port = _port()
    result = _run(
        f'. "{HELPER}"; ! reach_helper_up {port}',
        os.environ.copy(),
    )
    assert result.returncode == 0, result.stderr
