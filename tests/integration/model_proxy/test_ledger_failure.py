from threading import Event

import pytest

from harbor_research_runner.model_proxy.ledger import LedgerUnavailable, LedgerWriter
from harbor_research_runner.records.proxy_ledger import ProxyStart


def test_ledger_periodically_syncs_without_close(tmp_path):
    fsynced = Event()
    writer = LedgerWriter(
        tmp_path / "ledger.jsonl",
        sync_interval_s=0.05,
        fsync=lambda fd: fsynced.set(),
    )
    writer.write(ProxyStart(0, 0.0, "v", "a" * 64, False))

    assert fsynced.wait(timeout=1)
    writer.close()


def test_ledger_write_failure_stops_serving(tmp_path):
    path = tmp_path / "ledger.jsonl"
    path.write_bytes(b"")
    path.chmod(0o444)
    before = path.read_bytes()

    with pytest.raises(LedgerUnavailable):
        LedgerWriter(path)

    assert path.read_bytes() == before
