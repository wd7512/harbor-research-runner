"""Canonical token-usage alias map for chat and Responses shapes."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError, OverflowError):
        logger.warning("ignoring non-numeric usage value %r", value)
        return 0


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def normalize_usage_dict(raw: Any) -> dict[str, int]:
    """Coerce an already-extracted usage dict to ledger fields."""
    raw = _as_dict(raw)
    prompt_details = _as_dict(raw.get("prompt_tokens_details"))
    input_details = _as_dict(raw.get("input_tokens_details"))
    if "cached_read_tokens" in raw or "cache_read_tokens" in raw:
        cached = raw.get("cached_read_tokens", raw.get("cache_read_tokens", 0))
    else:
        cached = prompt_details.get("cached_tokens", input_details.get("cached_tokens", 0))
    return {
        "prompt_tokens": as_int(raw.get("prompt_tokens", raw.get("input_tokens", 0))),
        "completion_tokens": as_int(raw.get("completion_tokens", raw.get("output_tokens", 0))),
        "cached_read_tokens": as_int(cached),
        "prompt_chars": as_int(raw.get("prompt_chars", 0)),
        "completion_chars": as_int(raw.get("completion_chars", 0)),
    }


def normalize_usage(obj: Any) -> dict[str, int] | None:
    """Normalize a response envelope to ledger fields, None when absent."""
    if not isinstance(obj, dict):
        return None
    raw = obj.get("usage")
    if not isinstance(raw, dict):
        response = obj.get("response")
        raw = response.get("usage") if isinstance(response, dict) else None
    if not isinstance(raw, dict):
        return None
    return normalize_usage_dict(raw)
