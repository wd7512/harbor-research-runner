from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest

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


def test_build_copies_docker_oom_events(tmp_path: Path) -> None:
    run_dir = _copy_run("clean", tmp_path)
    events = b'{"Type":"container","Action":"oom"}\n'
    (run_dir / "docker-oom-events.jsonl").write_bytes(events)
    output = tmp_path / "bundle"

    assert build(run_dir, output, {}) == 0
    assert (output / "docker-oom-events.jsonl").read_bytes() == events


def test_build_copies_memory_watch(tmp_path: Path) -> None:
    run_dir = _copy_run("clean", tmp_path)
    samples = b'{"type":"sample","trial":"trial-a"}\n'
    (run_dir / "memory-watch.jsonl").write_bytes(samples)
    output = tmp_path / "bundle"

    assert build(run_dir, output, {}) == 0
    assert (output / "memory-watch.jsonl").read_bytes() == samples


def test_build_skips_unjudged_trials_without_finish(tmp_path: Path) -> None:
    run_dir = _copy_run("retries", tmp_path)
    output = tmp_path / "bundle"

    assert build(run_dir, output, {}) == 0
    rows = [json.loads(line) for line in (output / "trials.jsonl").read_text().splitlines()]
    assert all(row["trial"] != "unfinished" for row in rows)
    assert "| Trials not yet judged | 1 |" in (output / "report.md").read_text()
    assert (output / "manifest.json").is_file()


def test_build_refuses_unjudged_trials_after_finish(tmp_path: Path) -> None:
    run_dir = _copy_run("retries", tmp_path)
    events_path = run_dir / "run-events.jsonl"
    with events_path.open("a", encoding="utf-8") as stream:
        stream.write(
            json.dumps(
                {
                    "type": "finish",
                    "t": 20,
                    "exit_code": 3,
                    "slots_complete": 1,
                    "slots_incomplete": 1,
                }
            )
            + "\n"
        )
    output = tmp_path / "bundle"

    with pytest.raises(ValueError, match="unfinished"):
        build(run_dir, output, {})

    assert not output.exists()


def test_build_ignores_malformed_ledger_accounting_fields(tmp_path: Path) -> None:
    run_dir = _copy_run("clean", tmp_path)
    events_path = run_dir / "run-events.jsonl"
    events = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines()]
    verdict = next(row for row in events if row.get("trial") == "a-alpha-trial")
    verdict.update(measured=False, unmeasured_reason="ledger_damaged")
    events_path.write_text(
        "".join(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n" for row in events),
        encoding="utf-8",
    )
    ledger_path = run_dir / "proxy-ledger.jsonl"
    rows = [json.loads(line) for line in ledger_path.read_text(encoding="utf-8").splitlines()]
    malformed = next(row for row in rows if row.get("type") == "request_end")
    malformed["prompt_tokens"] = "10"
    valid = dict(malformed)
    valid.update(seq=11, request_id="extra-valid-request", prompt_tokens=4)
    rows.append(valid)
    ledger_path.write_text(
        "".join(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n" for row in rows),
        encoding="utf-8",
    )
    output = tmp_path / "bundle"

    assert build(run_dir, output, {}) == 0
    trial = next(
        json.loads(line)
        for line in (output / "trials.jsonl").read_text(encoding="utf-8").splitlines()
        if json.loads(line)["trial"] == "a-alpha-trial"
    )
    assert trial["prompt_tokens"] == 4
    assert trial["measured"] is False
    assert trial["unmeasured_reason"] == "ledger_damaged"


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
                "prompt_chars": 0,
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
            "prompt_chars": 0,
            "upstream_status": None,
        }
    ]


def test_merged_requests_ignores_malformed_accounting_fields() -> None:
    rows = merged_requests(
        [
            {
                "type": "request_start",
                "seq": 4,
                "t": 1.0,
                "trial": "trial-a",
                "request_id": "request-a",
                "model": "fixture/model",
                "prompt_chars": 0,
            },
            {
                "type": "request_end",
                "seq": 5,
                "t": 2.0,
                "trial": "trial-a",
                "request_id": "request-a",
                "upstream_status": "ok",
                "prompt_tokens": "10",
                "completion_tokens": 3,
                "cached_read_tokens": 0,
                "completion_chars": 12,
                "first_content_s": 0.5,
                "duration_s": 1.0,
            },
        ]
    )

    assert rows[0]["upstream_status"] is None


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
                    "prompt_chars": 0,
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
                    "upstream_status": "ok",
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "cached_read_tokens": 0,
                    "completion_chars": 0,
                    "first_content_s": None,
                    "duration_s": 0.0,
                }
            )
            + "\n"
        )
    output = tmp_path / "bundle"
    assert build(run_dir, output, {"FIXTURE_KEY": secret}) == 4
    assert not output.exists()
