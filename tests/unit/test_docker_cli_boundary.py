import re
from pathlib import Path

from harbor_research_runner.docker_host import __file__ as docker_host_init


def test_docker_argv_literals_are_confined_to_docker_host() -> None:
    package_root = Path(docker_host_init).parent.parent
    docker_argv = re.compile(r"\[\s*(['\"])docker\1\s*,")
    violations = []
    for path in sorted(package_root.rglob("*.py")):
        if "docker_host" in path.relative_to(package_root).parts:
            continue
        if docker_argv.search(path.read_text(encoding="utf-8")):
            violations.append(str(path.relative_to(package_root)))
    assert violations == []
