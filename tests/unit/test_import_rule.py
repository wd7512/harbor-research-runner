import re
from pathlib import Path

from harbor_research_runner.docker_host import __file__ as docker_host_init
from tests.support.import_rule import HARBOR_IMPORTERS, _violations


def test_checker_detects_a_step_importing_a_step() -> None:
    assert _violations("evidence_bundle/build.py", "from harbor_research_runner.run import loop")


def test_checker_detects_a_helper_importing_a_step() -> None:
    assert _violations("records/jsonl.py", "from harbor_research_runner.run import loop")


def test_checker_detects_records_importing_job_config() -> None:
    assert _violations("records/jsonl.py", "from harbor_research_runner import job_config")


def test_checker_detects_a_relative_import_of_a_forbidden_module() -> None:
    assert _violations("records/jsonl.py", "from ..job_config import job_file")


def test_checker_allows_a_module_to_import_itself() -> None:
    assert not _violations(
        "records/proxy_ledger.py", "from harbor_research_runner.records import jsonl"
    )
    assert not _violations("records/proxy_ledger.py", "from . import jsonl")


def test_checker_allows_run_to_import_harness() -> None:
    assert not _violations("run/checks.py", "from harbor_research_runner.harness import manifest")


def test_checker_allows_harness_to_import_docker_host() -> None:
    assert not _violations(
        "harness/build.py", "from harbor_research_runner.docker_host import images"
    )


def test_checker_allows_harbor_adapter_to_import_trial_validity() -> None:
    assert not _violations(
        "harbor_adapter/job_folder.py",
        "from harbor_research_runner.trial_validity.verdict import TrialFacts",
    )


def test_checker_allows_harbor_adapter_to_import_docker_host() -> None:
    assert not _violations(
        "harbor_adapter/plugin.py",
        "from harbor_research_runner.docker_host.resources import spare_memory_mb",
    )


def test_checker_allows_cli_to_import_the_model_proxy() -> None:
    assert not _violations(
        "cli.py", "from harbor_research_runner.model_proxy.adapters import select_adapter"
    )


def test_checker_allows_cli_to_import_any_module() -> None:
    assert not _violations("cli.py", "from harbor_research_runner.run import loop")


def test_checker_detects_run_importing_the_model_proxy() -> None:
    assert _violations("run/loop.py", "import harbor_research_runner.model_proxy.server")


def test_checker_detects_harbor_imported_outside_the_three_allowed_files() -> None:
    assert _violations("run/loop.py", "from harbor.job import Job")
    assert _violations("harness/manifest.py", "import harbor")


def test_checker_allows_harbor_in_the_three_allowed_files() -> None:
    for path in sorted(HARBOR_IMPORTERS):
        assert not _violations(path, "from harbor.agents.base import BaseAgent")


def test_checker_rejects_a_file_outside_the_architecture_modules() -> None:
    assert _violations("helpers/misc.py", "import json")


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
