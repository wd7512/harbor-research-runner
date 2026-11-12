from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from harbor_research_runner.model_proxy.adapters.base import (
    DecodeResult,
    StreamTranslator,
    UpstreamAdapter,
    normalize_usage,
)
from harbor_research_runner.model_proxy.chars import chat_delta_chars


def _usage_from_chunk(raw: str) -> dict[str, int] | None:
    if not raw or raw == "[DONE]":
        return None
    try:
        return normalize_usage(json.loads(raw))
    except json.JSONDecodeError:
        return None


class PassthroughTranslator(StreamTranslator):
    def __init__(self, on_usage: Callable[[dict[str, int]], None]):
        self._on_usage = on_usage
        self._done = False
        self._completion_chars = 0

    def feed_line(self, line: str) -> list[str]:
        if line.startswith("data:"):
            raw = line[5:].strip()
            if raw == "[DONE]":
                self._done = True
            else:
                usage = _usage_from_chunk(raw)
                if usage:
                    self._on_usage(usage)
                try:
                    obj = json.loads(raw)
                except json.JSONDecodeError:
                    obj = None
                if isinstance(obj, dict):
                    self._completion_chars += chat_delta_chars(obj)
        return [line + "\n"]

    @property
    def finished(self) -> bool:
        return self._done

    @property
    def completion_chars(self) -> int:
        return self._completion_chars


class ChatAdapter(UpstreamAdapter):
    def build_request(
        self, payload: dict[str, Any], model: str
    ) -> tuple[dict[str, Any], list[str]]:
        rewritten = dict(payload)
        rewritten["model"] = model
        return rewritten, []

    def decode_response(self, upstream_body: bytes, model: str, content_type: str) -> DecodeResult:
        try:
            usage = normalize_usage(json.loads(upstream_body))
        except json.JSONDecodeError:
            usage = None
        return DecodeResult(upstream_body, None, content_type, usage)

    def new_stream_translator(
        self, model: str, on_usage: Callable[[dict[str, int]], None]
    ) -> StreamTranslator:
        return PassthroughTranslator(on_usage)
