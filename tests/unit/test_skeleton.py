from importlib import import_module
from importlib.metadata import version

import pytest

MODULES = (
    "records",
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


def test_package_version_comes_from_the_installed_metadata():
    assert version("harbor-research-runner")


@pytest.mark.parametrize("name", MODULES)
def test_every_module_of_the_architecture_is_importable(name: str):
    assert import_module(f"harbor_research_runner.{name}").__doc__
