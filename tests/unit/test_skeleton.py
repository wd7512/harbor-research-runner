import tomllib
from importlib import import_module
from importlib.metadata import version
from pathlib import Path

import pytest

import harbor_research_runner

MODULES = (
    "records",
    "local_http",
    "job_config",
    "trial_validity",
    "statistics",
    "harbor_adapter",
    "docker_host",
    "harness",
    "run",
    "model_proxy",
    "evidence_bundle",
    "results_explorer",
)


def test_package_version_matches_project_and_lockfile():
    root = Path(__file__).parents[2]
    project = tomllib.loads((root / "pyproject.toml").read_text())
    lock = tomllib.loads((root / "uv.lock").read_text())
    locked_package = next(
        package for package in lock["package"] if package["name"] == project["project"]["name"]
    )
    project_version = project["project"]["version"]

    assert version(project["project"]["name"]) == project_version
    assert locked_package["version"] == project_version
    package_version = getattr(harbor_research_runner, "__version__", None)
    if package_version is not None:
        assert package_version == project_version


@pytest.mark.parametrize("name", MODULES)
def test_every_module_of_the_architecture_is_importable(name: str):
    assert import_module(f"harbor_research_runner.{name}").__doc__
