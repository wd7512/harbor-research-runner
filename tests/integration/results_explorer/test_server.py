from __future__ import annotations

import hashlib
import json
import shutil
import threading
import urllib.error
import urllib.request
from pathlib import Path

from harbor_research_runner.evidence_bundle.build import build
from harbor_research_runner.results_explorer.loader import EvidenceStore
from harbor_research_runner.results_explorer.server import build_server

FIXTURES = Path(__file__).parents[2] / "fixtures" / "run_folders"


def _bundle(source: str, root: Path) -> Path:
    run_dir = root / f"run-{source}"
    shutil.copytree(FIXTURES / source, run_dir)
    metadata_path = run_dir / "run-metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["machine_check_sha256"] = hashlib.sha256(
        (run_dir / "machine-check.json").read_bytes()
    ).hexdigest()
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    bundle = root / f"bundle-{source}"
    assert build(run_dir, bundle, {}) == 0
    return bundle


def test_api_routes_are_read_only_and_return_evidence(tmp_path: Path) -> None:
    first = _bundle("clean", tmp_path)
    second = _bundle("retries", tmp_path)
    httpd = build_server(EvidenceStore([first, second]), port=0)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    try:
        routes = (
            "/",
            "/api/evidence",
            "/api/evidence/clean/summary",
            "/api/evidence/clean/trials",
            "/api/evidence/clean/trials/a-alpha-trial/requests",
            "/api/evidence/clean/over-time",
            "/api/compare?run=clean&run=retries&a=alpha&b=beta&view=final",
            "/api/evidence/clean/trials.csv",
        )
        responses = {}
        for route in routes:
            with urllib.request.urlopen(base + route) as response:
                body = response.read()
                assert response.status == 200
                if route != "/":
                    if route.endswith(".csv"):
                        responses[route] = body.decode("utf-8")
                    else:
                        responses[route] = json.loads(body)
        assert responses["/api/evidence"]
        over_time = responses["/api/evidence/clean/over-time"]
        assert set(over_time) == {"all", "by_arm"}
        assert set(over_time["by_arm"]) == {
            "alpha",
            "beta",
        }
        assert len(responses["/api/evidence/clean/trials"]) > 0
        assert (
            len(responses["/api/evidence/clean/trials.csv"].splitlines())
            == len(responses["/api/evidence/clean/trials"]) + 1
        )
        try:
            urllib.request.urlopen(urllib.request.Request(base + "/api/evidence", method="POST"))
        except urllib.error.HTTPError as exc:
            assert exc.code == 405
        else:
            raise AssertionError("POST must be rejected")
        try:
            urllib.request.urlopen(base + "/api/evidence/missing/summary")
        except urllib.error.HTTPError as exc:
            assert exc.code == 404
        else:
            raise AssertionError("unknown run id must be rejected")
    finally:
        httpd.shutdown()
        thread.join(timeout=5)
        httpd.server_close()
