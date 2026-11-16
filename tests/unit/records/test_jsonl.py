import os

from harbor_research_runner.records.jsonl import (
    JsonlTail,
    ReadResult,
    append_newline,
    append_row,
    bad_line_time_spans,
    open_for_append,
    read_rows,
)


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


def test_append_newline_separates_damaged_tail(tmp_path):
    path = tmp_path / "rows.jsonl"
    path.write_bytes(b"not-json")
    fd = open_for_append(path)
    append_newline(fd)
    os.close(fd)

    assert path.read_bytes() == b"not-json\n"


def test_jsonl_tail_resets_after_truncation(tmp_path):
    path = tmp_path / "rows.jsonl"
    path.write_text('{"type":"old-row"}\n', encoding="utf-8")
    tail = JsonlTail(path)

    assert tail.read().rows == [{"type": "old-row"}]

    path.write_text('{"type":"new"}\n', encoding="utf-8")

    assert tail.read().rows == [{"type": "new"}]


def test_jsonl_tail_classifies_damaged_lines_like_read_rows(tmp_path):
    path = tmp_path / "rows.jsonl"
    path.write_text('{"type":"first"}\nnot-json\n{"type":"last"}\n', encoding="utf-8")
    tail = JsonlTail(path)

    assert tail.read() == read_rows(path)


def test_bad_line_time_span_uses_nearest_valid_timestamps():
    result = ReadResult(
        [{"t": 1.0}, {"t": 4.0}, {"t": 8.0}, {"t": 12.0}],
        False,
        [3],
    )

    assert bad_line_time_spans(result, finished=False) == [(4.0, 8.0)]


def test_bad_line_time_span_ignores_invalid_neighbor_timestamps():
    result = ReadResult(
        [{"t": True}, {"t": "bad"}, {"t": 8.0}],
        False,
        [2],
    )

    assert bad_line_time_spans(result, finished=False) == [(float("-inf"), 8.0)]


def test_bad_line_time_span_scales_timestamp_fields():
    result = ReadResult([{"timeNano": 1_000_000_000}, {"timeNano": 3_000_000_000}], False, [2])

    assert bad_line_time_spans(result, "timeNano", 1e-9, finished=False) == [(1.0, 3.0)]


def test_bad_line_time_span_without_neighboring_timestamp_uses_infinite_bounds():
    result = ReadResult([{"t": "bad"}, {"t": 2.0}], False, [1])

    assert bad_line_time_spans(result, finished=False) == [(float("-inf"), 2.0)]


def test_bad_line_time_span_without_following_timestamp_uses_positive_infinity():
    result = ReadResult([{"t": 2.0}], False, [2])

    assert bad_line_time_spans(result, finished=False) == [(2.0, float("inf"))]


def test_torn_final_line_spans_after_last_row_only_when_finished():
    result = ReadResult([{"t": 2.0}, {"t": 5.0}], True, [])

    assert bad_line_time_spans(result, finished=True) == [(5.0, float("inf"))]
    assert bad_line_time_spans(result, finished=False) == []


def test_finished_torn_final_line_without_rows_spans_from_negative_infinity():
    result = ReadResult([], True, [])

    assert bad_line_time_spans(result, finished=True) == [(float("-inf"), float("inf"))]
