from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

QUOTA_HORIZON_S = 900

STRONG_QUOTA_MARKERS = ("usagelimit",)
QUOTA_PHRASE_MARKERS = ("usage limit", "usage-limit")
KNOWN_BURST_MARKERS = ("ratelimit", "rate limit", "rate-limit")

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class UpstreamOutcome:
    http_status: int | None
    retry_after_s: float | None
    error_body: str
    got_terminator: bool
    cut_by_proxy: str | None
    transport_failed: bool
    client_disconnected: bool


def retry_after_seconds(value: str | None, now: datetime | None = None) -> float | None:
    """Seconds until the 429 resets, or None when the header is unusable."""
    if value is None:
        return None
    try:
        text = value.strip()
        if not text:
            return None
        if re.fullmatch(r"\d+", text):
            seconds = int(text)
            if seconds > 31_536_000:
                return None
            return float(seconds)
        moment = parsedate_to_datetime(text)
        if moment is None:
            return None
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)
        reference = now if now is not None else datetime.now(UTC)
        if reference.tzinfo is None:
            reference = reference.replace(tzinfo=UTC)
        return max(0.0, (moment - reference).total_seconds())
    except Exception:
        return None


def classify(outcome: UpstreamOutcome) -> str:
    if outcome.client_disconnected:
        return "client_disconnect"
    if outcome.cut_by_proxy is not None:
        return "stalled"
    if outcome.transport_failed:
        return "transport_error"
    status = outcome.http_status
    if status == 429:
        return _classify_429(outcome.error_body, outcome.retry_after_s)
    if status is not None and 500 <= status <= 599:
        return "server_error"
    if status is not None and 400 <= status <= 499:
        return "client_error"
    if status == 200:
        return "ok" if outcome.got_terminator else "truncated"
    return "server_error"


def status_for_harness(upstream_status: str, upstream_http_status: int | None) -> int:
    if upstream_status == "stalled":
        return 504
    if upstream_status == "transport_error":
        return 502
    if upstream_http_status is None:
        return 502
    return upstream_http_status


def _classify_429(body_text: str, retry_after_s: float | None) -> str:
    if retry_after_s is not None:
        return "quota_exhausted" if retry_after_s >= QUOTA_HORIZON_S else "rate_limited"
    lowered = body_text.lower()
    if any(marker in lowered for marker in STRONG_QUOTA_MARKERS):
        return "quota_exhausted"
    if any(marker in lowered for marker in QUOTA_PHRASE_MARKERS):
        return "quota_exhausted"
    if any(marker in lowered for marker in KNOWN_BURST_MARKERS):
        return "rate_limited"
    snippet = " ".join(body_text.split())[:160] or "<empty body>"
    logger.warning(
        "unrecognized 429 (Retry-After=%r, body=%r); classifying as rate_limited",
        retry_after_s,
        snippet,
    )
    return "rate_limited"
