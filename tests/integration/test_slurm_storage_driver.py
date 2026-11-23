from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).parents[2]
ENGINE_SCRIPT = ROOT / "scripts/slurm/docker-engine.sh"


def _run_engine(
    tmp_path: Path, storage_driver: str, fallback_mode: str
) -> subprocess.CompletedProcess[str]:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    work = tmp_path / "work"
    work.mkdir()
    (fake_bin / "dockerd-rootless.sh").write_text(
        "#!/bin/bash\n"
        "driver=\n"
        "while [ $# -gt 0 ]; do\n"
        '  if [ "$1" = --storage-driver ]; then driver=$2; shift 2; else shift; fi\n'
        "done\n"
        'printf \'%s\\n\' "$driver" >>"$ENGINE_CALLS"\n'
        'printf \'%s\\n\' "$driver" >"$ENGINE_DRIVER"\n'
        'if [ "$driver" = overlay2 ] && [ "$ENGINE_MODE" = fallback ]; then exit 1; fi\n'
        "while :; do sleep 0.05; done\n",
        encoding="utf-8",
    )
    (fake_bin / "docker").write_text(
        "#!/bin/bash\n"
        '[ "$1" = info ] || exit 1\n'
        'driver=$(cat "$ENGINE_DRIVER" 2>/dev/null || true)\n'
        'if [ "$driver" = overlay2 ] && [ "$ENGINE_MODE" = fallback ]; then exit 1; fi\n'
        'if [ "${2:-}" = --format ]; then printf \'%s\\n\' "$driver"; fi\n',
        encoding="utf-8",
    )
    for executable in (fake_bin / "dockerd-rootless.sh", fake_bin / "docker"):
        executable.chmod(0o755)
    script = f"""
set -u
work={str(work)!r}
log={str(tmp_path / "engine.log")!r}
source {str(ENGINE_SCRIPT)!r}
fetch_docker_release() {{ :; }}
ensure_compose_plugin() {{ :; }}
start_docker_engine
printf 'selected=%s storage=%s calls=' "$engine_selected" "${{engine_storage_driver:-unset}}"
cat "$ENGINE_CALLS"
stop_docker_engine
"""
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fake_bin}:{env['PATH']}",
            "HRR_ENGINE": "rootless-docker",
            "HRR_STORAGE_DRIVER": storage_driver,
            "ENGINE_MODE": fallback_mode,
            "ENGINE_CALLS": str(tmp_path / "calls"),
            "ENGINE_DRIVER": str(tmp_path / "driver"),
        }
    )
    return subprocess.run(
        ["bash", "-c", script], text=True, capture_output=True, env=env, timeout=4, check=False
    )


def test_auto_falls_back_to_vfs_when_overlay2_is_unavailable(tmp_path: Path) -> None:
    result = _run_engine(tmp_path, "auto", "fallback")

    assert result.returncode == 0, result.stderr
    assert "selected=rootless-docker storage=vfs calls=overlay2\nvfs" in result.stdout
    assert "falling back from overlay2 to vfs" in (tmp_path / "engine.log").read_text(
        encoding="utf-8"
    )


def test_auto_keeps_overlay2_when_it_is_available(tmp_path: Path) -> None:
    result = _run_engine(tmp_path, "auto", "overlay")

    assert result.returncode == 0, result.stderr
    assert result.stdout == "selected=rootless-docker storage=overlay2 calls=overlay2\n"


def test_explicit_vfs_does_not_fall_back_or_try_another_driver(tmp_path: Path) -> None:
    result = _run_engine(tmp_path, "vfs", "fallback")

    assert result.returncode == 0, result.stderr
    assert result.stdout == "selected=rootless-docker storage=vfs calls=vfs\n"
