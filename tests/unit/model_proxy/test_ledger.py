import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from harbor_research_runner.model_proxy.ledger import LedgerUnavailable, LedgerWriter
from harbor_research_runner.records.jsonl import read_rows
from harbor_research_runner.records.proxy_ledger import (
    Close,
    Open,
    ProxyStart,
    Refused,
    RequestEnd,
    RequestStart,
    sequence_gaps,
)

_RECORDS = (
    ProxyStart(0, 0.0, "v", "a" * 64, False),
    Open(0, 0.0, "trial", "task", "arm"),
    RequestStart(0, 0.0, "trial", "request", "model", True, 12, 0.25),
    RequestEnd(
        0,
        0.0,
        "trial",
        "request",
        200,
        "ok",
        200,
        "served",
        10,
        5,
        2,
        8,
        0.1,
        0.2,
        1.0,
        0,
        None,
    ),
    Refused(0, 0.0, "trial", "closed"),
    Close(0, 0.0, "trial"),
)


def test_sequence_increases_across_threads(tmp_path):
    path = tmp_path / "ledger.jsonl"
    writer = LedgerWriter(path, now=lambda: 1.0)

    def write_row(index):
        return writer.write(ProxyStart(0, 0.0, str(index), "a" * 64, False))

    with ThreadPoolExecutor(max_workers=8) as executor:
        sequences = list(executor.map(write_row, range(200)))
    writer.close()

    result = read_rows(path)
    assert sorted(sequences) == list(range(1, 201))
    assert len(result.rows) == 200
    assert result.bad_lines == []
    assert not result.torn_last_line
    assert sequence_gaps(result.rows) == []


def test_written_row_uses_injected_clock(tmp_path):
    path = tmp_path / "ledger.jsonl"
    writer = LedgerWriter(path, now=lambda: 123.5)

    writer.write(ProxyStart(0, 0.0, "v", "a" * 64, False))
    writer.close()

    assert read_rows(path).rows[0]["t"] == 123.5


@pytest.mark.parametrize("row", _RECORDS)
def test_written_row_matches_assigned_record(tmp_path, row):
    path = tmp_path / "ledger.jsonl"
    writer = LedgerWriter(path, now=lambda: 123.5)

    writer.write(row)
    writer.close()

    assert read_rows(path).rows == [replace(row, seq=1, t=123.5).to_row()]


def test_sequence_continues_after_reopening(tmp_path):
    path = tmp_path / "ledger.jsonl"
    writer = LedgerWriter(path, now=lambda: 1.0)
    for index in range(7):
        writer.write(ProxyStart(0, 0.0, str(index), "a" * 64, False))
    writer.close()

    reopened = LedgerWriter(path, now=lambda: 2.0)
    sequence = reopened.write(ProxyStart(0, 0.0, "next", "a" * 64, False))
    reopened.close()

    assert sequence == 8


def test_sequence_ignores_rows_without_an_integer_sequence(tmp_path):
    path = tmp_path / "ledger.jsonl"
    path.write_text(
        '{"type":"row_without_seq"}\n'
        '{"seq":"99","type":"string_seq"}\n'
        '{"seq":7,"type":"numbered"}\n',
        encoding="utf-8",
    )
    writer = LedgerWriter(path, now=lambda: 2.0)

    sequence = writer.write(ProxyStart(0, 0.0, "next", "a" * 64, False))
    writer.close()

    assert sequence == 8


def test_reopening_torn_last_line_starts_next_row_on_new_line(tmp_path):
    path = tmp_path / "ledger.jsonl"
    path.write_bytes(
        b'{"seq":7,"t":1.0,"type":"proxy_start","proxy_version":"v",'
        b'"config_sha256":"hash","restart":false}\n'
        b'{"seq":8,"type":"torn"'
    )
    writer = LedgerWriter(path, now=lambda: 2.0)

    writer.write(ProxyStart(0, 0.0, "next", "a" * 64, False))
    writer.close()

    result = read_rows(path)
    assert result.rows[-1] == ProxyStart(8, 2.0, "next", "a" * 64, False).to_row()
    assert result.bad_lines == [2]


def test_reopening_complete_final_row_without_newline_keeps_next_row_parseable(tmp_path):
    path = tmp_path / "ledger.jsonl"
    path.write_bytes(
        b'{"seq":7,"t":1.0,"type":"proxy_start","proxy_version":"v",'
        b'"config_sha256":"hash","restart":false}'
    )
    writer = LedgerWriter(path, now=lambda: 2.0)

    sequence = writer.write(ProxyStart(0, 0.0, "next", "a" * 64, False))
    writer.close()

    result = read_rows(path)
    assert sequence == 8
    assert result.rows[-1] == ProxyStart(8, 2.0, "next", "a" * 64, False).to_row()
    assert result.bad_lines == []
    assert not result.torn_last_line


def test_close_row_is_synced_before_write_returns(tmp_path):
    fsynced = threading.Event()
    fsync_threads = []

    def record_fsync(fd):
        fsync_threads.append(threading.get_ident())
        fsynced.set()

    caller_thread = threading.get_ident()
    writer = LedgerWriter(tmp_path / "ledger.jsonl", fsync=record_fsync)

    writer.write(Close(0, 0.0, "trial"))

    assert fsynced.is_set()
    assert fsync_threads[0] != caller_thread
    writer.close()


def test_fsync_does_not_block_other_writes(tmp_path):
    fsync_entered = threading.Event()
    release_fsync = threading.Event()
    request_finished = threading.Event()

    def blocked_fsync(fd):
        fsync_entered.set()
        release_fsync.wait()

    writer = LedgerWriter(tmp_path / "ledger.jsonl", sync_interval_s=0.01, fsync=blocked_fsync)
    with ThreadPoolExecutor(max_workers=2) as executor:
        close_write = executor.submit(writer.write, Close(0, 0.0, "trial"))
        assert fsync_entered.wait(timeout=1)

        def write_request():
            sequence = writer.write(RequestStart(0, 0.0, "trial", "request", "model", True, 1, 0.0))
            request_finished.set()
            return sequence

        request_write = executor.submit(write_request)
        finished_during_fsync = request_finished.wait(timeout=0.05)
        release_fsync.set()
        assert close_write.result(timeout=1) == 1
        assert request_write.result(timeout=1) == 2
    writer.close()

    assert finished_during_fsync


def test_failing_fsync_marks_writer_unavailable(tmp_path):
    def failing_fsync(fd):
        raise OSError("sync failed")

    writer = LedgerWriter(tmp_path / "ledger.jsonl", sync_interval_s=0.01, fsync=failing_fsync)
    with pytest.raises(LedgerUnavailable):
        writer.write(Close(0, 0.0, "trial"))

    assert writer.failed
    with pytest.raises(LedgerUnavailable):
        writer.write(ProxyStart(0, 0.0, "next", "a" * 64, False))
    with pytest.raises(LedgerUnavailable):
        writer.close()


def test_close_is_idempotent_and_write_after_close_fails(tmp_path):
    writer = LedgerWriter(tmp_path / "ledger.jsonl")
    writer.close()
    writer.close()

    with pytest.raises(LedgerUnavailable):
        writer.write(ProxyStart(0, 0.0, "v", "a" * 64, False))


def test_missing_parent_directory_makes_construction_unavailable(tmp_path):
    with pytest.raises(LedgerUnavailable):
        LedgerWriter(tmp_path / "missing" / "ledger.jsonl")


def test_history_reads_rows_written_after_construction(tmp_path):
    path = tmp_path / "ledger.jsonl"
    writer = LedgerWriter(path, now=lambda: 1.0)
    writer.write(ProxyStart(0, 0.0, "v", "a" * 64, False))

    assert writer.history().rows == [ProxyStart(1, 1.0, "v", "a" * 64, False).to_row()]
    writer.close()


def test_history_returns_a_fresh_snapshot(tmp_path):
    path = tmp_path / "ledger.jsonl"
    writer = LedgerWriter(path, now=lambda: 1.0)
    writer.write(ProxyStart(0, 0.0, "v", "a" * 64, False))

    first = writer.history()
    first.rows.clear()

    assert writer.history().rows == [ProxyStart(1, 1.0, "v", "a" * 64, False).to_row()]
    writer.close()


def test_history_after_restart_returns_every_row(tmp_path):
    path = tmp_path / "ledger.jsonl"
    writer = LedgerWriter(path, now=lambda: 1.0)
    writer.write(ProxyStart(0, 0.0, "v", "a" * 64, False))
    writer.write(Close(0, 0.0, "trial"))
    writer.close()

    reopened = LedgerWriter(path, now=lambda: 2.0)

    assert [row["seq"] for row in reopened.history().rows] == [1, 2]
    reopened.close()


def test_history_raises_when_ledger_file_is_missing(tmp_path):
    path = tmp_path / "ledger.jsonl"
    writer = LedgerWriter(path, now=lambda: 1.0)
    path.unlink()

    with pytest.raises(LedgerUnavailable):
        writer.history()

    writer.close()
