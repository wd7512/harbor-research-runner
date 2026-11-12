import hashlib
import json
import shutil
from pathlib import Path

from harbor_research_runner.evidence_bundle.build import build
from harbor_research_runner.evidence_bundle.manifest import verify_manifest

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


def test_manifest_reports_a_changed_file(tmp_path: Path) -> None:
    run_dir = _copy_run("clean", tmp_path)
    output = tmp_path / "bundle"
    assert build(run_dir, output, {}) == 0
    assert verify_manifest(output) == []
    trials = output / "trials.jsonl"
    trials.write_bytes(trials.read_bytes() + b" ")
    assert verify_manifest(output) == ["trials.jsonl: hash mismatch", "trials.jsonl: size mismatch"]


def test_manifest_reports_missing_and_extra_files(tmp_path: Path) -> None:
    run_dir = _copy_run("clean", tmp_path)
    output = tmp_path / "bundle"
    assert build(run_dir, output, {}) == 0
    (output / "report.md").unlink()
    (output / "extra.txt").write_text("extra", encoding="utf-8")
    assert verify_manifest(output) == ["extra.txt: extra file", "report.md: missing file"]
