import ast
import importlib.util
from pathlib import Path

PACKAGE = "harbor_research_runner"
SOURCE_ROOT = Path(__file__).parents[2] / "src" / PACKAGE

MAY_IMPORT = {
    "records": set(),
    "job_config": set(),
    "trial_validity": {"records"},
    "statistics": {"records"},
    "harbor_adapter": {"records", "job_config", "trial_validity"},
    "docker_host": {"records"},
    "harness": {"records"},
    "run": {
        "job_config",
        "records",
        "trial_validity",
        "harbor_adapter",
        "docker_host",
        "harness",
    },
    "model_proxy": {"records"},
    "evidence_bundle": {"records", "statistics", "harbor_adapter"},
    "results_explorer": {"records", "statistics"},
    "cli": {
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
    },
}

HARBOR_IMPORTERS = {
    "harness/agents.py",
    "harness/fake.py",
    "harbor_adapter/plugin.py",
}


def _module_name(path: str) -> str:
    return ".".join((PACKAGE, *Path(path).with_suffix("").parts))


def _imported_modules(tree: ast.AST, module_name: str) -> set[str]:
    imported: set[str] = set()
    if module_name.endswith(".__init__"):
        package_name = module_name.removesuffix(".__init__")
    else:
        package_name = module_name.rpartition(".")[0]
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                relative_name = "." * node.level + (node.module or "")
                try:
                    resolved = importlib.util.resolve_name(relative_name, package_name)
                except (ImportError, ValueError):
                    continue
            else:
                resolved = node.module or ""
            if resolved == PACKAGE:
                imported.update(f"{PACKAGE}.{alias.name}" for alias in node.names)
            else:
                imported.add(resolved)
    return imported


def _internal_package(imported_name: str) -> str | None:
    prefix = f"{PACKAGE}."
    if imported_name.startswith(prefix):
        return imported_name[len(prefix) :].split(".", 1)[0]
    return None


def _violations(path: str, source: str) -> list[str]:
    tree = ast.parse(source)
    normalized_path = Path(path).as_posix()
    module_name = _module_name(normalized_path)
    source_module = Path(normalized_path.split("/", 1)[0]).stem
    if source_module == "__init__":
        return []
    if source_module not in MAY_IMPORT:
        return [f"{normalized_path} belongs to no module of the architecture"]
    allowed = MAY_IMPORT[source_module] | {source_module}
    violations = []

    for imported_name in sorted(_imported_modules(tree, module_name)):
        package = _internal_package(imported_name)
        if package is not None and package not in allowed:
            violations.append(f"{normalized_path} imports forbidden package {package}")
        if (
            imported_name == "harbor" or imported_name.startswith("harbor.")
        ) and normalized_path not in HARBOR_IMPORTERS:
            violations.append(f"{normalized_path} imports harbor")
        if package == "model_proxy" and source_module != "model_proxy":
            violations.append(f"{normalized_path} imports model_proxy")

    return violations


def test_package_imports_follow_the_architecture_rule() -> None:
    violations = []
    for path in sorted(SOURCE_ROOT.rglob("*.py")):
        relative_path = path.relative_to(SOURCE_ROOT).as_posix()
        violations.extend(_violations(relative_path, path.read_text(encoding="utf-8")))
    assert not violations, "\n".join(violations)


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


def test_checker_allows_harbor_adapter_to_import_trial_validity() -> None:
    assert not _violations(
        "harbor_adapter/job_folder.py",
        "from harbor_research_runner.trial_validity.verdict import TrialFacts",
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
