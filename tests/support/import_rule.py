import ast
import importlib.util
from pathlib import Path

PACKAGE = "harbor_research_runner"
SOURCE_ROOT = Path(__file__).parents[2] / "src" / PACKAGE

MAY_IMPORT = {
    "records": set(),
    "local_http": set(),
    "job_config": set(),
    "trial_validity": {"records"},
    "statistics": {"records"},
    "harbor_adapter": {"records", "job_config", "trial_validity", "docker_host"},
    "docker_host": {"records"},
    "harness": {"records", "docker_host"},
    "run": {
        "job_config",
        "records",
        "trial_validity",
        "harbor_adapter",
        "docker_host",
        "harness",
    },
    "model_proxy": {"records", "local_http"},
    "evidence_bundle": {"records", "statistics", "harbor_adapter"},
    "results_explorer": {"records", "statistics", "local_http"},
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
    "harbor_adapter/task_files.py",
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
        if package == "model_proxy" and source_module not in {"model_proxy", "cli"}:
            violations.append(f"{normalized_path} imports model_proxy")

    return violations
