from __future__ import annotations

import re
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from harbor.agents.installed.base import BaseInstalledAgent

from harbor_research_runner.model_proxy.control import TrialRegistry
from harbor_research_runner.model_proxy.ledger import LedgerUnavailable, LedgerWriter
from harbor_research_runner.records.proxy_control import (
    CLOSED_BODY,
    CloseMessage,
    OpenMessage,
)
from harbor_research_runner.records.proxy_ledger import Open

UPSTREAM_UNAVAILABLE_BODY = (
    '{"error": {"message": "upstream unavailable", "type": "upstream_unavailable"}}'
)


@pytest.fixture
def ledger_path(tmp_path: Path) -> Path:
    return tmp_path / "proxy-ledger.jsonl"


@pytest.fixture
def writer(ledger_path: Path):
    writer = LedgerWriter(ledger_path, now=lambda: 1.0)
    try:
        yield writer
    finally:
        writer.close()


def _open(registry: TrialRegistry, trial: str = "A") -> int:
    return registry.open(OpenMessage(trial, "task", "arm"))


def test_open_then_admit_is_true(writer):
    registry = TrialRegistry(writer)

    assert _open(registry) == 200
    assert registry.admit("A") is True


def test_admit_before_open_is_false_and_writes_not_open(writer):
    registry = TrialRegistry(writer)

    assert registry.admit("A") is False
    rows = writer.history().rows
    assert [(row["type"], row["trial"], row.get("reason")) for row in rows] == [
        ("refused", "A", "not_open")
    ]


def test_close_then_admit_is_false_with_closed_reason(writer):
    registry = TrialRegistry(writer)
    _open(registry)
    registry.close(CloseMessage("A"))

    assert registry.admit("A") is False
    rows = writer.history().rows
    assert rows[-1]["type"] == "refused"
    assert rows[-1]["reason"] == "closed"


def test_second_open_of_an_open_trial_writes_no_second_row(writer):
    registry = TrialRegistry(writer)

    assert _open(registry) == 200
    assert _open(registry) == 200
    assert [row["type"] for row in writer.history().rows] == ["open"]


def test_open_after_close_returns_409(writer):
    registry = TrialRegistry(writer)
    _open(registry)
    registry.close(CloseMessage("A"))

    assert _open(registry) == 409
    assert [row["type"] for row in writer.history().rows] == ["open", "close"]


def test_open_trials_counts_open_trials(writer):
    registry = TrialRegistry(writer)
    _open(registry, "A")
    _open(registry, "B")
    registry.close(CloseMessage("B"))

    assert registry.open_trials() == 1


def test_unknown_close_records_close_and_terminally_closes(writer):
    registry = TrialRegistry(writer)

    assert registry.close(CloseMessage("A")) == 200
    assert [row["type"] for row in writer.history().rows] == ["close"]
    assert _open(registry) == 409
    assert registry.open_trials() == 0
    assert registry.admit("A") is False
    assert writer.history().rows[-1]["reason"] == "closed"


def test_repeated_close_writes_no_duplicate_row(writer):
    registry = TrialRegistry(writer)
    _open(registry)

    assert registry.close(CloseMessage("A")) == 200
    assert registry.close(CloseMessage("A")) == 200
    assert [row["type"] for row in writer.history().rows] == ["open", "close"]


def test_failed_close_does_not_publish_closed_state(ledger_path):
    def failing_fsync(fd):
        raise OSError("sync failed")

    writer = LedgerWriter(ledger_path, now=lambda: 1.0, fsync=failing_fsync)
    registry = TrialRegistry(writer)
    _open(registry)

    with pytest.raises(LedgerUnavailable):
        registry.close(CloseMessage("A"))

    assert registry.open_trials() == 1
    with pytest.raises(LedgerUnavailable):
        registry.admit("A")
    with pytest.raises(LedgerUnavailable):
        writer.close()


def test_registry_rebuilt_over_ledger_admits_open_and_refuses_closed(ledger_path):
    first = LedgerWriter(ledger_path, now=lambda: 1.0)
    first_registry = TrialRegistry(first)
    first_registry.open(OpenMessage("A", "task", "arm"))
    first_registry.open(OpenMessage("B", "task", "arm"))
    first_registry.close(CloseMessage("B"))
    first.close()

    reopened = LedgerWriter(ledger_path, now=lambda: 2.0)
    rebuilt = TrialRegistry(reopened)

    assert rebuilt.open_trials() == 1
    assert rebuilt.admit("A") is True
    assert rebuilt.admit("B") is False
    assert reopened.history().rows[-1]["reason"] == "closed"
    reopened.close()


def test_later_open_row_does_not_reopen_a_closed_trial(ledger_path):
    first = LedgerWriter(ledger_path, now=lambda: 1.0)
    first_registry = TrialRegistry(first)
    first_registry.open(OpenMessage("A", "task", "arm"))
    first_registry.close(CloseMessage("A"))
    first.write(Open(0, 0.0, "A", "task", "arm"))
    first.close()

    reopened = LedgerWriter(ledger_path, now=lambda: 2.0)
    rebuilt = TrialRegistry(reopened)

    assert rebuilt.open_trials() == 0
    assert rebuilt.open(OpenMessage("A", "task", "arm")) == 409
    reopened.close()


@pytest.mark.parametrize("body", [CLOSED_BODY, UPSTREAM_UNAVAILABLE_BODY])
def test_control_refusal_body_avoids_harbor_error_pattern(body: str) -> None:
    for pattern in BaseInstalledAgent.ERROR_PATTERNS:
        assert re.search(pattern.pattern, body, re.IGNORECASE) is None


def test_replay_with_bad_line_raises_ledger_unavailable(ledger_path):
    ledger_path.write_bytes(
        b'{"arm":"arm","seq":1,"t":1.0,"task":"task","trial":"A","type":"open"}\n'
        b"not json\n"
        b'{"seq":3,"t":1.0,"trial":"A","type":"close"}\n'
    )
    writer = LedgerWriter(ledger_path, now=lambda: 1.0)

    with pytest.raises(LedgerUnavailable):
        TrialRegistry(writer)
    writer.close()


def test_replay_with_torn_line_raises_ledger_unavailable(ledger_path):
    ledger_path.write_bytes(
        b'{"arm":"arm","seq":1,"t":1.0,"task":"task","trial":"A","type":"open"}\n'
        b'{"seq":2,"t":1.0,"trial":"A","typ'
    )
    writer = LedgerWriter(ledger_path, now=lambda: 1.0)

    with pytest.raises(LedgerUnavailable):
        TrialRegistry(writer)
    writer.close()


def test_replay_with_malformed_row_raises_ledger_unavailable(ledger_path):
    ledger_path.write_bytes(
        b'{"arm":"arm","seq":1,"t":1.0,"task":"task","trial":"A","type":"open"}\n'
        b'{"seq":2,"t":1.0,"trial":"A","type":"mystery"}\n'
    )
    writer = LedgerWriter(ledger_path, now=lambda: 1.0)

    with pytest.raises(LedgerUnavailable):
        TrialRegistry(writer)
    writer.close()


def test_replay_with_sequence_gap_raises_ledger_unavailable(ledger_path):
    ledger_path.write_bytes(
        b'{"arm":"arm","seq":1,"t":1.0,"task":"task","trial":"A","type":"open"}\n'
        b'{"seq":3,"t":1.0,"trial":"A","type":"close"}\n'
    )
    writer = LedgerWriter(ledger_path, now=lambda: 1.0)

    with pytest.raises(LedgerUnavailable):
        TrialRegistry(writer)
    writer.close()


def test_replay_with_missing_initial_sequences_raises_ledger_unavailable(ledger_path):
    ledger_path.write_bytes(
        b'{"seq":2,"t":1.0,"trial":"A","type":"close"}\n'
        b'{"seq":3,"t":1.0,"trial":"B","type":"close"}\n'
    )
    writer = LedgerWriter(ledger_path, now=lambda: 1.0)

    with pytest.raises(LedgerUnavailable, match="proxy ledger replay failed"):
        TrialRegistry(writer)
    writer.close()


def test_replay_accepts_sequences_starting_at_one(ledger_path):
    ledger_path.write_bytes(
        b'{"seq":1,"t":1.0,"trial":"A","type":"close"}\n'
        b'{"seq":2,"t":1.0,"trial":"B","type":"close"}\n'
        b'{"seq":3,"t":1.0,"trial":"C","type":"close"}\n'
    )
    writer = LedgerWriter(ledger_path, now=lambda: 1.0)

    registry = TrialRegistry(writer)

    assert registry.open_trials() == 0
    assert registry.admit("A") is False
    writer.close()


def test_failed_writer_rejects_registry_calls_even_on_idempotent_paths(ledger_path):
    fsync_calls = 0

    def failing_fsync(fd):
        nonlocal fsync_calls
        fsync_calls += 1
        if fsync_calls > 1:
            raise OSError("sync failed")

    writer = LedgerWriter(ledger_path, now=lambda: 1.0, fsync=failing_fsync)
    registry = TrialRegistry(writer)
    _open(registry, "A")
    registry.close(CloseMessage("B"))

    with pytest.raises(LedgerUnavailable):
        registry.close(CloseMessage("A"))

    for operation in (
        lambda: registry.admit("A"),
        lambda: registry.open(OpenMessage("A", "task", "arm")),
        lambda: registry.close(CloseMessage("B")),
    ):
        with pytest.raises(LedgerUnavailable):
            operation()
    with pytest.raises(LedgerUnavailable):
        writer.close()


def test_closed_writer_rejects_registry_calls_even_on_idempotent_paths(writer):
    registry = TrialRegistry(writer)
    _open(registry, "A")
    registry.close(CloseMessage("B"))
    writer.close()

    for operation in (
        lambda: registry.admit("A"),
        lambda: registry.open(OpenMessage("A", "task", "arm")),
        lambda: registry.close(CloseMessage("B")),
    ):
        with pytest.raises(LedgerUnavailable):
            operation()


def test_healthy_writer_keeps_idempotent_registry_paths_available(writer):
    registry = TrialRegistry(writer)
    _open(registry, "A")
    registry.close(CloseMessage("B"))

    assert registry.admit("A") is True
    assert _open(registry, "A") == 200
    assert registry.close(CloseMessage("B")) == 200


def test_concurrent_opens_write_one_open_row(writer):
    registry = TrialRegistry(writer)
    barrier = threading.Barrier(16)

    def open_trial(_):
        barrier.wait()
        return _open(registry)

    with ThreadPoolExecutor(max_workers=16) as executor:
        results = list(executor.map(open_trial, range(16)))

    assert results == [200] * 16
    assert [row["type"] for row in writer.history().rows] == ["open"]


def test_concurrent_closes_write_one_close_row(writer):
    registry = TrialRegistry(writer)
    _open(registry)
    barrier = threading.Barrier(16)

    def close_trial(_):
        barrier.wait()
        return registry.close(CloseMessage("A"))

    with ThreadPoolExecutor(max_workers=16) as executor:
        results = list(executor.map(close_trial, range(16)))

    assert results == [200] * 16
    assert [row["type"] for row in writer.history().rows] == ["open", "close"]


def test_concurrent_open_and_close_replay_to_live_closed_state(writer, ledger_path):
    registry = TrialRegistry(writer)
    barrier = threading.Barrier(32)

    def open_trial(_):
        barrier.wait()
        return _open(registry)

    def close_trial(_):
        barrier.wait()
        return registry.close(CloseMessage("A"))

    with ThreadPoolExecutor(max_workers=32) as executor:
        opens = [executor.submit(open_trial, index) for index in range(16)]
        closes = [executor.submit(close_trial, index) for index in range(16)]
        results = [future.result() for future in opens + closes]

    rows = writer.history().rows
    close_index = next(index for index, row in enumerate(rows) if row["type"] == "close")
    assert all(row["type"] != "open" for row in rows[close_index + 1 :])
    assert all(result in (200, 409) for result in results)
    assert registry.admit("A") is False

    rebuilt = TrialRegistry(writer)
    assert rebuilt.open_trials() == registry.open_trials() == 0
    assert rebuilt.admit("A") is False


def test_replay_accepts_complete_final_row_without_newline(ledger_path):
    ledger_path.write_bytes(
        b'{"arm":"arm","seq":1,"t":1.0,"task":"task","trial":"A","type":"open"}\n'
        b'{"seq":2,"t":1.0,"trial":"A","type":"close"}'
    )
    writer = LedgerWriter(ledger_path, now=lambda: 1.0)

    registry = TrialRegistry(writer)

    assert registry.open_trials() == 0
    assert registry.admit("A") is False
    assert writer.history().rows[-1]["reason"] == "closed"
    writer.close()
