from __future__ import annotations

import shlex
from pathlib import Path


def stub_docker_dir(tmp_path: Path, calls_path: Path) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$1\" >> {shlex.quote(str(calls_path))}\n"
        'if [ "$1" = events ]; then exec sleep 30; fi\n'
        "exit 0\n",
        encoding="utf-8",
    )
    docker.chmod(0o755)
    return bin_dir
