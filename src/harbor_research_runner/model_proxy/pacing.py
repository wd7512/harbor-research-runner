from __future__ import annotations

import json
import math
import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

_WINDOW_S = 60.0


def _content_chars(content: Any) -> int:
    if content is None:
        return 0
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        total = 0
        for part in content:
            if isinstance(part, str):
                total += len(part)
            elif isinstance(part, dict):
                text = part.get("text")
                total += len(text) if isinstance(text, str) else len(json.dumps(part, default=str))
        return total
    return len(json.dumps(content, default=str))


def estimate_prompt_tokens(body: dict) -> int:
    """Estimate prompt tokens from chat or Responses input, including tools."""
    chars = 0
    messages = body.get("messages")
    if isinstance(messages, list):
        for message in messages:
            if isinstance(message, dict):
                chars += _content_chars(message.get("content")) + 4
    inputs = body.get("input")
    if isinstance(inputs, list):
        for item in inputs:
            if isinstance(item, dict):
                chars += _content_chars(item.get("content"))
    tools = body.get("tools")
    if tools:
        chars += len(json.dumps(tools, default=str))
    return max(0, math.ceil(chars / 4))


def payload_completion_cap(body: dict) -> int | None:
    """Return the positive completion cap declared by the request, if any."""
    for key in ("max_tokens", "max_completion_tokens", "max_output_tokens"):
        value = body.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return value
    return None


def expected_completion_draw(max_output_tokens: int | None, body: dict) -> int:
    """Estimate completion tokens using the request cap and model ceiling."""
    ceiling = (
        max_output_tokens
        if isinstance(max_output_tokens, int)
        and not isinstance(max_output_tokens, bool)
        and max_output_tokens > 0
        else None
    )
    cap = payload_completion_cap(body)
    if cap is not None and ceiling is not None:
        return min(cap, ceiling)
    if cap is not None:
        return cap
    return ceiling or 0


def estimate_request_tokens(body: dict, max_output_tokens: int | None) -> int:
    """Estimate total request tokens as prompt tokens plus completion draw."""
    return estimate_prompt_tokens(body) + expected_completion_draw(max_output_tokens, body)


@dataclass
class _Reservation:
    t: float
    tokens: int


class Pacer:
    def __init__(
        self,
        tokens_per_minute: int | None,
        now: Callable[[], float],
        sleep: Callable[[float], None],
        cooldown_until: float | None = None,
    ) -> None:
        self._limit = tokens_per_minute
        self._now = now
        self._sleep = sleep
        self._cooldown_until = cooldown_until
        self._reservations: deque[_Reservation] = deque()
        self._unrecorded: deque[_Reservation] = deque()
        self._lock = threading.Lock()

    def wait_for(self, estimated_tokens: int) -> float:
        """Pace a reservation; concurrent waiters are not guaranteed FIFO order."""
        waited = 0.0
        cost = min(max(0, int(estimated_tokens)), self._limit) if self._limit is not None else 0
        while True:
            delay = 0.0
            with self._lock:
                now = self._now()
                if self._cooldown_until is not None and now < self._cooldown_until:
                    delay = self._cooldown_until - now
                else:
                    self._cooldown_until = None
                    if self._limit is None:
                        return waited
                    self._discard_expired(now)
                    used = sum(item.tokens for item in self._reservations)
                    if used + cost <= self._limit:
                        reservation = _Reservation(now, cost)
                        self._reservations.append(reservation)
                        self._unrecorded.append(reservation)
                        return waited
                    delay = self._reservations[0].t + _WINDOW_S - now
            self._sleep(delay)
            waited += delay

    def record(self, actual_tokens: int) -> None:
        """Replace the oldest estimate with actual prompt plus completion usage; pairing is FIFO."""
        with self._lock:
            if self._unrecorded:
                self._unrecorded.popleft().tokens = max(0, int(actual_tokens))

    def _discard_expired(self, now: float) -> None:
        while self._reservations and now - self._reservations[0].t >= _WINDOW_S:
            self._reservations.popleft()
