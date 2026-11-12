from __future__ import annotations

import gzip
import hashlib
import json
import os
import shutil
import tempfile
from collections.abc import Mapping
from pathlib import Path

from harbor_research_runner.evidence_bundle.manifest import write_manifest
from harbor_research_runner.evidence_bundle.report import render_report
from harbor_research_runner.evidence_bundle.secret_scan import key_values, scan
from harbor_research_runner.evidence_bundle.trials import build_trial_rows
from harbor_research_runner.records.jsonl import append_row, read_rows
from harbor_research_runner.records.machine_check import read as read_machine_check


def merged_requests(ledger_rows: list[dict]) -> list[dict]:
    starts: dict[str, dict] = {}
    ends: dict[str, dict] = {}
    for row in ledger_rows:
        if row.get("type") == "request_start":
            starts[row["request_id"]] = row
        elif row.get("type") == "request_end":
            ends[row["request_id"]] = row
    result = []
    for request_id, start in starts.items():
        end = ends.get(request_id)
        merged = dict(start)
        if end is None:
            merged["upstream_status"] = None
        else:
            merged.update(
                {key: value for key, value in end.items() if key not in {"type", "seq", "t"}}
            )
            merged["t_end"] = end["t"]
        result.append(merged)
    return sorted(result, key=lambda row: row["seq"])


def _jsonl(rows: list[dict]) -> bytes:
    with tempfile.TemporaryFile() as stream:
        for row in rows:
            append_row(stream.fileno(), row)
        stream.seek(0)
        return stream.read()


def _gzip(data: bytes) -> bytes:
    import io

    buffer = io.BytesIO()
    with gzip.GzipFile(fileobj=buffer, mode="wb", filename="", compresslevel=6, mtime=0) as stream:
        stream.write(data)
    return buffer.getvalue()


def _planned_slots(metadata: dict, rows: list) -> list[tuple[str, str]]:
    slots = {
        (task["name"], arm["label"])
        for task in metadata.get("task_set", {}).get("tasks", [])
        for arm in metadata["arms"]
    }
    slots.update((row.task, row.arm) for row in rows if row.location != "canary-job")
    return sorted(slots)


def _write_files(folder: Path, files: Mapping[str, bytes]) -> None:
    for name, data in files.items():
        path = folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)


def build(run_dir: Path, out_dir: Path, environment: Mapping[str, str]) -> int:
    metadata_path = run_dir / "run-metadata.json"
    metadata_bytes = metadata_path.read_bytes()
    metadata = json.loads(metadata_bytes)
    machine_check_path = run_dir / "machine-check.json"
    machine_check_bytes = machine_check_path.read_bytes()
    actual_digest = hashlib.sha256(machine_check_bytes).hexdigest()
    expected_digest = metadata["machine_check_sha256"]
    if actual_digest != expected_digest:
        raise ValueError(
            f"machine check sha256 mismatch: metadata has {expected_digest}, file has {actual_digest}"
        )
    machine_check = read_machine_check(machine_check_path)
    rows = build_trial_rows(run_dir)
    events_path = run_dir / "run-events.jsonl"
    events_bytes = events_path.read_bytes()
    events = read_rows(events_path).rows
    repetitions = metadata["job"]["repetitions"]
    planned_slots = _planned_slots(metadata, rows)
    report = render_report(
        rows,
        metadata,
        events,
        planned_slots,
        repetitions,
        machine_check=machine_check,
    )
    ledger_rows = read_rows(run_dir / "proxy-ledger.jsonl").rows
    resource_path = run_dir / "resource-stats.jsonl"
    resource_bytes = resource_path.read_bytes() if resource_path.exists() else b""
    files = {
        "trials.jsonl": _jsonl([row.to_row() for row in rows]),
        "proxy-requests.jsonl.gz": _gzip(_jsonl(merged_requests(ledger_rows))),
        "resource-stats.jsonl.gz": _gzip(resource_bytes),
        "run-events.jsonl": events_bytes,
        "run-metadata.json": metadata_bytes,
        "report.md": (report + "\n").encode("utf-8"),
    }
    if scan(files, key_values(metadata["model_card"], environment)):
        return 4

    out_dir.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(dir=out_dir.parent, prefix=f".{out_dir.name}."))
    backup = temporary.with_name(temporary.name + ".previous")
    try:
        _write_files(temporary, files)
        write_manifest(temporary, metadata["tool_version"], metadata["run_id"])
        if out_dir.exists():
            os.rename(out_dir, backup)
        try:
            os.rename(temporary, out_dir)
        except BaseException:
            if backup.exists() and not out_dir.exists():
                os.rename(backup, out_dir)
            raise
        if backup.exists():
            shutil.rmtree(backup)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
        if backup.exists():
            shutil.rmtree(backup)
    return 0
