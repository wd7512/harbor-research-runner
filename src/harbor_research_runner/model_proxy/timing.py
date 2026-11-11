from __future__ import annotations

import time
from collections.abc import Callable


class StreamClock:
    """Monotonic stamps for one streamed request."""

    def __init__(self, now: Callable[[], float] = time.monotonic) -> None:
        self._now = now
        self._start = now()
        self.first_chunk_s: float | None = None
        self.first_content_s: float | None = None
        self.duration_s: float | None = None

    def mark_chunk(self) -> None:
        if self.first_chunk_s is None:
            self.first_chunk_s = self._now() - self._start

    def mark_content(self) -> None:
        self.mark_chunk()
        if self.first_content_s is None:
            self.first_content_s = self._now() - self._start

    def finish(self) -> float:
        if self.duration_s is None:
            self.duration_s = self._now() - self._start
        return self.duration_s


def output_rate(
    completion_tokens: int | None,
    first_content_s: float | None,
    duration_s: float | None,
) -> float | None:
    """Completion tokens per second over the content window, when measurable."""
    if completion_tokens is None or first_content_s is None or duration_s is None:
        return None
    window = duration_s - first_content_s
    if window < 0.5:
        return None
    return completion_tokens / window
