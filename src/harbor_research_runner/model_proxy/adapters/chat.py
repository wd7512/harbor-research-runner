from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from harbor_research_runner.model_proxy.adapters.base import (
    DecodeResult,
    StreamTranslator,
    UpstreamAdapter,
    UpstreamDecodeError,
    normalize_usage,
)
from harbor_research_runner.model_proxy.chars import chat_completion_chars, chat_delta_chars


class PassthroughTranslator(StreamTranslator):
    def __init__(self, on_usage: Callable[[dict[str, int]], None]):
        self._on_usage = on_usage
        self._done = False
        self._terminal = False
        self._completion_chars = 0
        self._usage: dict[str, int] | None = None
        self._served_model: str | None = None
        self._data_lines: list[str] = []
        self._observed_payload: str | None = None

    def feed_line(self, line: str) -> list[str]:
        if line == "":
            self._observe_event()
        elif line.startswith("data:"):
            if self._observed_payload is not None:
                self._data_lines.clear()
                self._observed_payload = None
            self._data_lines.append(line[5:].lstrip())
            self._observe_payload()
        return [line + "\n"]

    def _observe_event(self) -> None:
        if not self._data_lines:
            return
        self._observe_payload()
        self._data_lines.clear()
        self._observed_payload = None

    def _observe_payload(self) -> None:
        raw = "\n".join(self._data_lines).strip()
        if not raw or raw == self._observed_payload:
            return
        if raw == "[DONE]":
            self._done = True
            self._terminal = True
            self._observed_payload = raw
            return
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            return
        self._observed_payload = raw
        if not isinstance(obj, dict):
            return
        usage = normalize_usage(obj)
        if usage is not None:
            self._usage = usage
            self._on_usage(usage)
        choices = obj.get("choices")
        if isinstance(choices, list):
            if (
                any(
                    isinstance(choice, dict) and choice.get("finish_reason") is not None
                    for choice in choices
                )
                or not choices
                and usage is not None
            ):
                self._terminal = True
        elif usage is not None:
            self._terminal = True
        model = obj.get("model")
        if isinstance(model, str):
            self._served_model = model
        self._completion_chars += chat_delta_chars(obj)

    @property
    def finished(self) -> bool:
        return self._done

    @property
    def completion_chars(self) -> int:
        return self._completion_chars

    @property
    def usage(self) -> dict[str, int] | None:
        return self._usage

    @property
    def served_model(self) -> str | None:
        return self._served_model

    @property
    def terminal(self) -> bool:
        return self._terminal


class ChatAdapter(UpstreamAdapter):
    @property
    def passthrough(self) -> bool:
        return True

    def build_request(
        self, payload: dict[str, Any], model: str
    ) -> tuple[dict[str, Any], list[str]]:
        rewritten = dict(payload)
        rewritten["model"] = model
        return rewritten, []

    def decode_response(self, upstream_body: bytes, model: str, content_type: str) -> DecodeResult:
        try:
            payload = json.loads(upstream_body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise UpstreamDecodeError("upstream body is not JSON") from None
        if not isinstance(payload, dict):
            raise UpstreamDecodeError("upstream body is not a JSON object")
        served_model = payload.get("model")
        return DecodeResult(
            upstream_body,
            None,
            content_type,
            normalize_usage(payload),
            served_model if isinstance(served_model, str) else None,
            chat_completion_chars(payload),
        )

    def new_stream_translator(
        self, model: str, on_usage: Callable[[dict[str, int]], None]
    ) -> StreamTranslator:
        return PassthroughTranslator(on_usage)
