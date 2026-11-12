from __future__ import annotations

import hashlib
import json
from pathlib import Path


def write_manifest(out_dir: Path, tool_version: str, run_id: str) -> None:
    files = []
    for path in sorted(item for item in out_dir.rglob("*") if item.is_file()):
        if path.name == "manifest.json" and path.parent == out_dir:
            continue
        data = path.read_bytes()
        files.append(
            {
                "name": path.relative_to(out_dir).as_posix(),
                "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        )
    manifest = {"tool_version": tool_version, "run_id": run_id, "files": files}
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8"
    )


def verify_manifest(out_dir: Path) -> list[str]:
    try:
        manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
        declared = {item["name"]: item for item in manifest["files"]}
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError):
        return ["manifest.json: missing or invalid manifest"]

    actual = {
        path.relative_to(out_dir).as_posix(): path
        for path in out_dir.rglob("*")
        if path.is_file() and path != out_dir / "manifest.json"
    }
    problems = []
    for name in sorted(declared.keys() - actual.keys()):
        problems.append(f"{name}: missing file")
    for name in sorted(actual.keys() - declared.keys()):
        problems.append(f"{name}: extra file")
    for name in sorted(declared.keys() & actual.keys()):
        data = actual[name].read_bytes()
        record = declared[name]
        if len(data) != record.get("bytes"):
            problems.append(f"{name}: size mismatch")
        if hashlib.sha256(data).hexdigest() != record.get("sha256"):
            problems.append(f"{name}: hash mismatch")
    return sorted(problems)
