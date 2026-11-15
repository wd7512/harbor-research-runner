from __future__ import annotations

import shutil
from pathlib import Path

from tests.e2e.conftest import collect_run_diagnostics

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "run_folders"


def test_run_diagnostics_include_the_last_lines_from_each_log(tmp_path: Path) -> None:
    run_dir = tmp_path / "sample-run"
    shutil.copytree(FIXTURES / "clean", run_dir)
    event_lines = [f"event-{index}" for index in range(50)]
    runner_lines = [f"runner-{index}" for index in range(70)]
    ledger_lines = [f"ledger-{index}" for index in range(30)]
    (run_dir / "run-events.jsonl").write_text("\n".join(event_lines), encoding="utf-8")
    (run_dir / "runner.log").write_text("\n".join(runner_lines), encoding="utf-8")
    (run_dir / "proxy-ledger.jsonl").write_text("\n".join(ledger_lines), encoding="utf-8")

    diagnostics = collect_run_diagnostics(run_dir)

    assert diagnostics == (
        "run-events.jsonl (last 40 lines):\n" + "\n".join(event_lines[-40:]) + "\n\n"
        "runner.log (last 60 lines):\n" + "\n".join(runner_lines[-60:]) + "\n\n"
        "proxy-ledger.jsonl (last 20 lines):\n" + "\n".join(ledger_lines[-20:])
    )
