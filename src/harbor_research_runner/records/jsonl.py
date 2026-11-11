import json
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ReadResult:
    rows: list[dict]
    torn_last_line: bool
    bad_lines: list[int]


def open_for_append(path: Path) -> int:
    return os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)


def append_row(fd: int, row: dict) -> None:
    line = json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n"
    os.write(fd, line.encode("utf-8"))


def read_rows(path: Path) -> ReadResult:
    try:
        content = path.read_bytes()
    except FileNotFoundError:
        return ReadResult([], False, [])

    if not content:
        return ReadResult([], False, [])

    lines = content.split(b"\n")
    ends_with_newline = content.endswith(b"\n")
    if ends_with_newline:
        lines.pop()
    torn_last_line = False
    bad_lines = []
    rows = []
    for index, raw_line in enumerate(lines, start=1):
        is_last = index == len(lines)
        has_newline = index < len(lines) or ends_with_newline
        if is_last and not has_newline:
            torn_last_line = True
        line = raw_line
        if line.endswith(b"\r"):
            line = line[:-1]
        try:
            row = json.loads(line.decode("utf-8"))
            if not isinstance(row, dict):
                raise ValueError("JSONL row is not an object")
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            if is_last:
                torn_last_line = True
            else:
                bad_lines.append(index)
        else:
            rows.append(row)

    return ReadResult(rows, torn_last_line, bad_lines)
