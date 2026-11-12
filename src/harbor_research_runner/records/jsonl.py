import json
import os
from contextlib import suppress
from dataclasses import dataclass, field
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


def append_newline(fd: int) -> None:
    os.write(fd, b"\n")


def _decode_row(raw_line: bytes) -> dict:
    line = raw_line[:-1] if raw_line.endswith(b"\r") else raw_line
    row = json.loads(line.decode("utf-8"))
    if not isinstance(row, dict):
        raise ValueError("JSONL row is not an object")
    return row


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
        try:
            row = _decode_row(raw_line)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            if is_last:
                torn_last_line = True
            else:
                bad_lines.append(index)
        else:
            rows.append(row)

    return ReadResult(rows, torn_last_line, bad_lines)


@dataclass
class JsonlTail:
    path: Path
    _offset: int = 0
    _pending: bytes = b""
    _rows: list[dict] = field(default_factory=list)
    _bad_lines: list[int] = field(default_factory=list)
    _physical_lines: int = 0
    _last_bad_line: int | None = None

    def read(self) -> ReadResult:
        try:
            if self.path.stat().st_size < self._offset:
                self._reset()
            with self.path.open("rb") as stream:
                stream.seek(self._offset)
                chunk = stream.read()
            self._offset += len(chunk)
        except FileNotFoundError:
            return self._result()

        lines = (self._pending + chunk).split(b"\n")
        self._pending = lines.pop()
        for raw_line in lines:
            self._physical_lines += 1
            if self._last_bad_line is not None:
                self._bad_lines.append(self._last_bad_line)
                self._last_bad_line = None
            try:
                row = _decode_row(raw_line)
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
                self._last_bad_line = self._physical_lines
            else:
                self._rows.append(row)
        if self._pending and self._last_bad_line is not None:
            self._bad_lines.append(self._last_bad_line)
            self._last_bad_line = None
        return self._result()

    def _result(self) -> ReadResult:
        rows = list(self._rows)
        if self._pending:
            with suppress(UnicodeDecodeError, json.JSONDecodeError, ValueError):
                rows.append(_decode_row(self._pending))
        return ReadResult(
            rows,
            bool(self._pending) or self._last_bad_line is not None,
            list(self._bad_lines),
        )

    def _reset(self) -> None:
        self._offset = 0
        self._pending = b""
        self._rows.clear()
        self._bad_lines.clear()
        self._physical_lines = 0
        self._last_bad_line = None
