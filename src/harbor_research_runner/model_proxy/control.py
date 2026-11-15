from __future__ import annotations

import threading

from harbor_research_runner.model_proxy.ledger import LedgerUnavailable, LedgerWriter
from harbor_research_runner.records import jsonl
from harbor_research_runner.records.proxy_control import CloseMessage, OpenMessage
from harbor_research_runner.records.proxy_ledger import (
    Close,
    Open,
    Refused,
    parse_row,
)


class TrialRegistry:
    """The proxy's record of which trials are open and which are closed."""

    def __init__(self, ledger: LedgerWriter) -> None:
        self._ledger = ledger
        self._lock = threading.Lock()
        self._open: set[str] = set()
        self._closed: set[str] = set()
        self._replay(ledger.history())

    def _replay(self, history: jsonl.ReadResult) -> None:
        if history.bad_lines or history.torn_last_line:
            raise LedgerUnavailable("proxy ledger replay failed")
        try:
            rows = [parse_row(row) for row in history.rows]
        except ValueError as exc:
            raise LedgerUnavailable("proxy ledger replay failed") from exc
        if [row.get("seq") for row in history.rows] != list(range(1, len(history.rows) + 1)):
            raise LedgerUnavailable("proxy ledger replay failed")
        for parsed in rows:
            if isinstance(parsed, Open):
                if parsed.trial not in self._closed:
                    self._open.add(parsed.trial)
            elif isinstance(parsed, Close):
                self._closed.add(parsed.trial)
                self._open.discard(parsed.trial)

    def open(self, message: OpenMessage) -> int:
        with self._lock:
            self._ledger.ensure_available()
            if message.trial in self._closed:
                return 409
            if message.trial in self._open:
                return 200
            self._ledger.write(Open(0, 0.0, message.trial, message.task, message.arm))
            self._open.add(message.trial)
            return 200

    def close(self, message: CloseMessage) -> int:
        with self._lock:
            self._ledger.ensure_available()
            if message.trial in self._closed:
                return 200
            self._ledger.write(Close(0, 0.0, message.trial))
            self._closed.add(message.trial)
            self._open.discard(message.trial)
            return 200

    def admit(self, trial: str) -> bool:
        with self._lock:
            self._ledger.ensure_available()
            if trial in self._open and trial not in self._closed:
                return True
            reason = "closed" if trial in self._closed else "not_open"
            self._ledger.write(Refused(0, 0.0, trial, reason))
            return False

    def open_trials(self) -> int:
        with self._lock:
            return len(self._open)
