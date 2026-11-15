from __future__ import annotations

import pytest

from scripts.mutation_pass import pytest_runtest_logreport


def test_wall_budget_breach_marks_mutant_unverified(monkeypatch, tmp_path):
    mutant = "harbor_research_runner.run.loop.x_run__mutmut_1"
    working_dir = tmp_path / "mutants"
    working_dir.mkdir()
    monkeypatch.chdir(working_dir)
    monkeypatch.setenv("MUTANT_UNDER_TEST", mutant)
    report = pytest.TestReport(
        nodeid="tests/integration/test_run.py::test_run",
        location=("tests/integration/test_run.py", 1, "test_run"),
        keywords={},
        outcome="failed",
        longrepr=(
            "tests/integration/test_run.py::test_run: setup+call+teardown used 5.100s "
            "of wall time, over the 5.00s budget"
        ),
        when="call",
        sections=[],
        duration=5.1,
        start=0.0,
        stop=5.1,
    )

    pytest_runtest_logreport(report)

    markers = list((tmp_path / ".agent" / "mutation-timeouts").glob("*.json"))
    assert len(markers) == 1
    assert markers[0].read_text(encoding="utf-8") == f'"{mutant}"'
