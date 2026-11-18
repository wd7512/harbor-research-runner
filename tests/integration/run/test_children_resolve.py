from pathlib import Path

from harbor_research_runner.run.checks import children_resolve_check


def test_children_resolve_fails_when_harbor_is_missing(tmp_path: Path) -> None:
    harbor = tmp_path / "harbor"

    ok, detail = children_resolve_check(harbor)

    assert not ok
    assert str(harbor) in detail


def test_children_resolve_passes_when_harbor_is_executable(tmp_path: Path) -> None:
    harbor = tmp_path / "harbor"
    harbor.write_text("#!/bin/sh\n", encoding="utf-8")
    harbor.chmod(0o755)

    ok, detail = children_resolve_check(harbor)

    assert ok
    assert "Harbor" in detail
