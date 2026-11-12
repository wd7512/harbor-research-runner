from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

from harbor_research_runner.evidence_bundle.build import build, merged_requests

FIXTURES = Path(__file__).parents[2] / "fixtures" / "run_folders"


def _copy_run(source: str, destination: Path) -> Path:
    run_dir = destination / source
    shutil.copytree(FIXTURES / source, run_dir)
    metadata_path = run_dir / "run-metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["machine_check_sha256"] = hashlib.sha256(
        (run_dir / "machine-check.json").read_bytes()
    ).hexdigest()
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    return run_dir


def _hashes(folder: Path) -> dict[str, str]:
    return {
        path.relative_to(folder).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(folder.rglob("*"))
        if path.is_file()
    }


def test_build_is_byte_deterministic_for_both_run_shapes(tmp_path: Path) -> None:
    for fixture in ("clean", "retries"):
        run_dir = _copy_run(fixture, tmp_path)
        first, second = tmp_path / f"{fixture}-first", tmp_path / f"{fixture}-second"
        assert build(run_dir, first, {}) == 0
        assert build(run_dir, second, {}) == 0
        assert _hashes(first) == _hashes(second)
        assert (first / "manifest.json").is_file()


def test_merged_requests_keeps_a_start_without_an_end() -> None:
    rows = merged_requests(
        [
            {
                "type": "request_start",
                "seq": 4,
                "t": 1.0,
                "trial": "trial-a",
                "request_id": "request-a",
                "model": "fixture/model",
            }
        ]
    )
    assert rows == [
        {
            "type": "request_start",
            "seq": 4,
            "t": 1.0,
            "trial": "trial-a",
            "request_id": "request-a",
            "model": "fixture/model",
            "upstream_status": None,
        }
    ]


def test_secret_in_ledger_refuses_without_writing_bundle(tmp_path: Path) -> None:
    run_dir = _copy_run("clean", tmp_path)
    secret = "fixture-secret-value-61"
    metadata_path = run_dir / "run-metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["model_card"]["key_env"] = ["FIXTURE_KEY"]
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    with (run_dir / "proxy-ledger.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(
            json.dumps(
                {
                    "type": "request_start",
                    "seq": 100,
                    "t": 2,
                    "trial": "secret-trial",
                    "request_id": "secret-request",
                }
            )
            + "\n"
        )
        stream.write(
            json.dumps(
                {
                    "type": "request_end",
                    "seq": 101,
                    "t": 3,
                    "trial": "secret-trial",
                    "request_id": "secret-request",
                    "error_excerpt": secret,
                }
            )
            + "\n"
        )
    output = tmp_path / "bundle"
    assert build(run_dir, output, {"FIXTURE_KEY": secret}) == 4
    assert not output.exists()
