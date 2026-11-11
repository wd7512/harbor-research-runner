import os

from harbor_research_runner.records.jsonl import append_row, open_for_append, read_rows


def test_round_trip_three_rows(tmp_path):
    path = tmp_path / "rows.jsonl"
    fd = open_for_append(path)
    rows = [{"type": "one", "n": 1}, {"type": "two", "n": 2}, {"type": "three", "n": 3}]
    for row in rows:
        append_row(fd, row)
    os.close(fd)

    result = read_rows(path)

    assert result.rows == rows
    assert result.torn_last_line is False
    assert result.bad_lines == []


def test_torn_last_line_is_tolerated(tmp_path):
    path = tmp_path / "rows.jsonl"
    path.write_text('{"type":"ok"}\n{"type":"unfinished"', encoding="utf-8")

    result = read_rows(path)

    assert result.rows == [{"type": "ok"}]
    assert result.torn_last_line is True
    assert result.bad_lines == []


def test_valid_last_row_without_newline_is_reported_torn(tmp_path):
    path = tmp_path / "rows.jsonl"
    path.write_text('{"type":"row"}', encoding="utf-8")

    result = read_rows(path)

    assert result.rows == [{"type": "row"}]
    assert result.torn_last_line is True


def test_damaged_middle_line_is_reported(tmp_path):
    path = tmp_path / "rows.jsonl"
    path.write_text('{"type":"first"}\nnot-json\n{"type":"last"}\n', encoding="utf-8")

    result = read_rows(path)

    assert result.rows == [{"type": "first"}, {"type": "last"}]
    assert result.torn_last_line is False
    assert result.bad_lines == [2]


def test_missing_file_is_empty(tmp_path):
    result = read_rows(tmp_path / "missing.jsonl")

    assert result.rows == []
    assert result.torn_last_line is False
    assert result.bad_lines == []


def test_keys_are_written_sorted(tmp_path):
    path = tmp_path / "rows.jsonl"
    fd = open_for_append(path)
    append_row(fd, {"z": 1, "type": "row", "a": 2})
    os.close(fd)

    assert path.read_text(encoding="utf-8") == '{"a":2,"type":"row","z":1}\n'
