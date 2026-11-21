import os
import subprocess
from pathlib import Path


def _docker(tmp_path: Path, images: str = "") -> tuple[Path, dict[str, str]]:
    log = tmp_path / "docker-argv"
    stdin = tmp_path / "docker-stdin"
    docker = tmp_path / "docker"
    docker.write_text(
        "#!/bin/sh\n"
        'printf "%s\\n" "$*" >>"$DOCKER_LOG"\n'
        'case "$1" in\n'
        '  login) cat >"$DOCKER_STDIN" ;;\n'
        '  image) printf "%s\\n" "$DOCKER_IMAGES" ;;\n'
        "  save) printf archive ;;\n"
        "  load) : ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    docker.chmod(0o755)
    environment = os.environ | {
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "DOCKER_LOG": str(log),
        "DOCKER_STDIN": str(stdin),
        "DOCKER_IMAGES": images,
    }
    return log, environment


def test_registry_login_refuses_file_mode_644(tmp_path: Path) -> None:
    log, environment = _docker(tmp_path)
    credentials = tmp_path / "credentials"
    credentials.write_text("docker.io user token\n", encoding="utf-8")
    credentials.chmod(0o644)
    result = subprocess.run(
        [
            "bash",
            "-c",
            '. scripts/slurm/registry.sh; registry_login "$1"',
            "bash",
            str(credentials),
        ],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
        timeout=5,
    )

    assert result.returncode == 1
    assert "mode 600" in result.stderr
    assert not log.exists()


def test_registry_login_sends_token_only_on_stdin(tmp_path: Path) -> None:
    log, environment = _docker(tmp_path)
    credentials = tmp_path / "credentials"
    credentials.write_text("docker.io user secret-token\n", encoding="utf-8")
    credentials.chmod(0o600)

    result = subprocess.run(
        [
            "bash",
            "-c",
            '. scripts/slurm/registry.sh; registry_login "$1"',
            "bash",
            str(credentials),
        ],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
        timeout=5,
    )

    assert result.returncode == 0
    assert (tmp_path / "docker-stdin").read_text(encoding="utf-8") == "secret-token\n"
    assert "secret-token" not in log.read_text(encoding="utf-8")
    assert "secret-token" not in result.stdout + result.stderr


def test_registry_login_refuses_malformed_line(tmp_path: Path) -> None:
    log, environment = _docker(tmp_path)
    credentials = tmp_path / "credentials"
    credentials.write_text("docker.io user token extra\n", encoding="utf-8")
    credentials.chmod(0o600)

    result = subprocess.run(
        [
            "bash",
            "-c",
            '. scripts/slurm/registry.sh; registry_login "$1"',
            "bash",
            str(credentials),
        ],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
        timeout=5,
    )

    assert result.returncode == 1
    assert "exactly three fields" in result.stderr
    assert not log.exists()


def test_image_cache_saves_tagged_images_and_loads_each_tar(tmp_path: Path) -> None:
    log, environment = _docker(tmp_path, "repo/one:v1\nrepo/two:v2\n<none>:<none>")
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "repo_one_v1.tar").write_text("existing", encoding="utf-8")

    result = subprocess.run(
        [
            "bash",
            "-c",
            '. scripts/slurm/registry.sh; save_image_cache "$1"; load_image_cache "$1"',
            "bash",
            str(cache),
        ],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
        timeout=5,
    )

    assert result.returncode == 0
    assert sorted(path.name for path in cache.glob("*.tar")) == [
        "repo_one_v1.tar",
        "repo_two_v2.tar",
    ]
    assert (cache / "repo_two_v2.tar").read_text(encoding="utf-8") == "archive"
    assert not list(cache.glob("*.tmp"))
    calls = log.read_text(encoding="utf-8").splitlines()
    assert calls.count("save repo/two:v2") == 1
    assert sum(call.startswith("load -i ") for call in calls) == 2
    assert "image cache: loaded 2" in result.stdout
