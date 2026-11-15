import os
import threading
import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

from harbor_research_runner.records import jsonl
from harbor_research_runner.records.proxy_ledger import Close


class LedgerUnavailable(RuntimeError):
    pass


class LedgerWriter:
    def __init__(
        self,
        path: Path,
        now: Callable[[], float] = time.time,
        sync_interval_s: float = 5.0,
        fsync: Callable[[int], None] = os.fsync,
    ) -> None:
        self._path = path
        self._now = now
        self._sync_interval_s = sync_interval_s
        self._fsync = fsync
        try:
            result = jsonl.read_rows(path)
        except OSError as exc:
            raise LedgerUnavailable("proxy ledger unavailable") from exc
        try:
            fd = jsonl.open_for_append(path)
        except OSError as exc:
            raise LedgerUnavailable("proxy ledger unavailable") from exc
        if result.torn_last_line:
            try:
                jsonl.append_newline(fd)
            except OSError as exc:
                os.close(fd)
                raise LedgerUnavailable("proxy ledger unavailable") from exc
        self._fd = fd
        self._seq = max(
            (row["seq"] for row in result.rows if type(row.get("seq")) is int),
            default=0,
        )
        self._failed = False
        self._lock = threading.Lock()
        self._condition = threading.Condition(self._lock)
        self._wake = threading.Event()
        self._sync_requested = 0
        self._sync_completed = 0
        self._closed = False
        self._stop = False
        self._thread = threading.Thread(target=self._sync_loop, daemon=True)
        self._thread.start()

    @property
    def failed(self) -> bool:
        with self._lock:
            return self._failed

    def ensure_available(self) -> None:
        """Raise when the ledger can no longer record proxy events."""
        with self._lock:
            if self._failed or self._closed:
                raise LedgerUnavailable("proxy ledger unavailable")

    def history(self) -> jsonl.ReadResult:
        """Return a fresh snapshot of every row in the ledger."""
        with self._lock:
            if not self._path.is_file():
                raise LedgerUnavailable("proxy ledger unavailable")
            try:
                return jsonl.read_rows(self._path)
            except OSError as exc:
                raise LedgerUnavailable("proxy ledger unavailable") from exc

    def write(self, row) -> int:
        """Append a row, making a Close row durable before this call returns."""
        with self._condition:
            if self._failed or self._closed:
                raise LedgerUnavailable("proxy ledger unavailable")
            seq = self._seq + 1
            assigned = replace(row, seq=seq, t=self._now())
            try:
                jsonl.append_row(self._fd, assigned.to_row())
            except OSError as exc:
                self._failed = True
                self._condition.notify_all()
                raise LedgerUnavailable("proxy ledger unavailable") from exc
            self._seq = seq
            if isinstance(assigned, Close):
                self._sync_requested += 1
                requested = self._sync_requested
                self._wake.set()
                while self._sync_completed < requested and not self._failed:
                    self._condition.wait()
                if self._failed:
                    raise LedgerUnavailable("proxy ledger unavailable")
            return seq

    def close(self) -> None:
        with self._condition:
            if self._closed:
                return
            self._closed = True
            self._sync_requested += 1
            requested = self._sync_requested
            self._stop = True
            self._wake.set()
        self._thread.join()
        with self._condition:
            os.close(self._fd)
            if self._failed or self._sync_completed < requested:
                raise LedgerUnavailable("proxy ledger unavailable")

    def _sync_loop(self) -> None:
        while True:
            self._wake.wait(self._sync_interval_s)
            self._wake.clear()
            with self._condition:
                if self._failed:
                    self._condition.notify_all()
                    return
                fd = self._fd
                requested = self._sync_requested
            try:
                self._fsync(fd)
            except OSError:
                with self._condition:
                    self._failed = True
                    self._condition.notify_all()
                return
            with self._condition:
                self._sync_completed = max(self._sync_completed, requested)
                self._condition.notify_all()
                if self._stop and self._sync_completed >= self._sync_requested:
                    return
