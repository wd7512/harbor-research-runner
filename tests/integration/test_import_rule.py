from tests.support.import_rule import SOURCE_ROOT, _violations


def test_package_imports_follow_the_architecture_rule() -> None:
    violations = []
    for path in sorted(SOURCE_ROOT.rglob("*.py")):
        relative_path = path.relative_to(SOURCE_ROOT).as_posix()
        violations.extend(_violations(relative_path, path.read_text(encoding="utf-8")))
    assert not violations, "\n".join(violations)
