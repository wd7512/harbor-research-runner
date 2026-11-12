import gzip
import logging
import os
import shutil
import threading
from _thread import LockType
from pathlib import Path

from harbor_research_runner.records import jsonl

logger = logging.getLogger(__name__)


class BodyStore:
    def __init__(self, directory: Path | None) -> None:
        self._directory = directory
        self._locks: dict[str, threading.Lock] = {}
        self._locks_lock = threading.Lock()
        self._closed_trials: set[str] = set()

    def write(self, trial: str, request_id: str, request_body: bytes, response_body: bytes) -> None:
        if self._directory is None:
            return
        lock = self._trial_lock(trial)
        if lock is None:
            return
        with lock:
            if trial in self._closed_trials:
                logger.warning("request body write refused for closed trial %r", trial)
                return
            path = self._directory / f"{trial}.jsonl"
            fd = None
            try:
                fd = jsonl.open_for_append(path)
                jsonl.append_row(
                    fd,
                    {
                        "request_id": request_id,
                        "request": request_body.decode("utf-8", errors="replace"),
                        "response": response_body.decode("utf-8", errors="replace"),
                    },
                )
            except Exception as exc:
                logger.warning("failed to store request bodies for trial %r: %s", trial, exc)
            finally:
                if fd is not None:
                    try:
                        os.close(fd)
                    except Exception as exc:
                        logger.warning(
                            "failed to close request body file for trial %r: %s", trial, exc
                        )

    def close_trial(self, trial: str) -> None:
        if self._directory is None:
            return
        lock = self._trial_lock(trial)
        if lock is None:
            return
        with lock:
            self._closed_trials.add(trial)
            plain_path = self._directory / f"{trial}.jsonl"
            compressed_path = self._directory / f"{trial}.jsonl.gz"
            try:
                if not plain_path.exists():
                    return
                with plain_path.open("rb") as source, compressed_path.open("wb") as target:
                    with gzip.GzipFile(
                        filename="", mode="wb", fileobj=target, mtime=0
                    ) as compressed:
                        shutil.copyfileobj(source, compressed)
                plain_path.unlink()
            except Exception as exc:
                logger.warning("failed to compress request bodies for trial %r: %s", trial, exc)

    def _trial_lock(self, trial: str) -> LockType | None:
        if not trial or "/" in trial or "\\" in trial or "\x00" in trial or trial.startswith("."):
            logger.warning("invalid request body trial name %r", trial)
            return None
        with self._locks_lock:
            lock = self._locks.get(trial)
            if lock is None:
                lock = threading.Lock()
                self._locks[trial] = lock
            return lock
