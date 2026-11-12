from __future__ import annotations

import threading
import urllib.request
from pathlib import Path

from harbor_research_runner.results_explorer.loader import EvidenceStore
from harbor_research_runner.results_explorer.server import build_server


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
    page = (
        Path(__file__).parents[3]
        / "src"
        / "harbor_research_runner"
        / "results_explorer"
        / "page.html"
    )

    assert page.is_file()
    for line_number, line in enumerate(page.read_text(encoding="utf-8").splitlines(), 1):
        assert len(line) <= 120, f"page.html:{line_number} has {len(line)} characters"
