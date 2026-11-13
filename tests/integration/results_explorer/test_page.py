from __future__ import annotations

import threading
import urllib.request
from pathlib import Path

import pytest

from harbor_research_runner.results_explorer.loader import EvidenceStore
from harbor_research_runner.results_explorer.server import build_server


def _page_path() -> Path:
    return (
        Path(__file__).parents[3]
        / "src"
        / "harbor_research_runner"
        / "results_explorer"
        / "page.html"
    )


def test_root_serves_the_self_contained_explorer_page() -> None:
    httpd = build_server(EvidenceStore([]), port=0)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/") as response:
            body = response.read()
            page = body.decode("utf-8")
            assert response.status == 200
            assert response.headers.get_content_type() == "text/html"
            assert len(body) < 200_000
            assert "http://" not in page
            assert "https://" not in page
            assert page.count("<script>") == 1
            assert "<script src=" not in page
    finally:
        httpd.shutdown()
        thread.join(timeout=5)
        httpd.server_close()


def test_page_source_lines_are_reviewable() -> None:
    page = _page_path()

    assert page.is_file()
    for line_number, line in enumerate(page.read_text(encoding="utf-8").splitlines(), 1):
        assert len(line) <= 120, f"page.html:{line_number} has {len(line)} characters"


def test_trial_view_command_uses_run_folder_and_names_job_and_trial() -> None:
    page = _page_path().read_text(encoding="utf-8")

    assert "harbor view ${escapeHtml(shellQuote(" in page
    assert r'path.replace(/\/+$/, "")' in page
    assert "Job: ${escapeHtml(state.trial.location)}" in page
    assert "· Trial:" in page
    assert "${escapeHtml(state.trial.trial)}</p>" in page
    assert r'${path.replace(/\/$/,"")}/${state.trial.location}/${state.trial.trial}' not in page


def test_harbor_view_detects_run_folder_and_rejects_trial_folder(tmp_path: Path) -> None:
    from harbor.cli.view import _detect_folder_type

    run_dir = tmp_path / "run"
    job_dir = run_dir / "harbor-job"
    trial_dir = job_dir / "trial-1"
    (job_dir / "config.json").parent.mkdir(parents=True)
    (job_dir / "config.json").write_text("{}", encoding="utf-8")
    trial_dir.mkdir()
    (trial_dir / "config.json").write_text("{}", encoding="utf-8")
    (trial_dir / "agent").mkdir()
    (trial_dir / "verifier").mkdir()

    assert _detect_folder_type(run_dir) == "jobs"
    with pytest.raises(SystemExit):
        _detect_folder_type(trial_dir)
