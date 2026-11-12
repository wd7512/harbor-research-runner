from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from harbor_research_runner.model_proxy.chars import (
    chat_completion_chars,
    chat_delta_chars,
    responses_delta_chars,
    responses_output_chars,
)
from harbor_research_runner.model_proxy.usage import normalize_usage

from .base import DecodeResult, StreamTranslator, UpstreamAdapter, UpstreamDecodeError
from .responses import ResponsesAdapter

Adapter = UpstreamAdapter


class UnsupportedAdapterPair(ValueError):
    def __init__(self, upstream_api: str, request_api: str):
        super().__init__(
            f"unsupported adapter pair: harness {request_api}, upstream {upstream_api}"
        )


class _IdentityStreamTranslator(StreamTranslator):
    def __init__(self, api: str, on_usage: Callable[[dict[str, int]], None]) -> None:
        self._api = api
        self._on_usage = on_usage
        self._finished = False
        self._completion_chars = 0
        self._usage: dict[str, int] | None = None
        self._served_model: str | None = None
        self._terminal = False

    def feed_line(self, line: str) -> list[str]:
        if not line.startswith("data:"):
            return [line + "\n"]
        raw = line[5:].strip()
        if self._api == "chat" and raw == "[DONE]":
            self._finished = True
            self._terminal = True
            return [line + "\n"]
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            return [line + "\n"]
        if not isinstance(obj, dict):
            return [line + "\n"]
        usage = normalize_usage(obj)
        if usage is not None:
            self._usage = usage
            self._on_usage(usage)
        model = obj.get("model")
        response = obj.get("response")
        if isinstance(response, dict) and isinstance(response.get("model"), str):
            model = response["model"]
        if isinstance(model, str):
            self._served_model = model
        if self._api == "chat":
            self._completion_chars += chat_delta_chars(obj)
        else:
            self._completion_chars += responses_delta_chars(obj)
            if obj.get("type") in {
                "response.completed",
                "response.incomplete",
                "response.failed",
            }:
                self._finished = True
                self._terminal = True
        if self._api == "chat":
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
        return [line + "\n"]

    @property
    def finished(self) -> bool:
        return self._finished

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


class _IdentityAdapter(UpstreamAdapter):
    def __init__(self, api: str):
        self._api = api

    @property
    def passthrough(self) -> bool:
        return True

    def build_request(
        self, payload: dict[str, Any], model: str
    ) -> tuple[dict[str, Any], list[str]]:
        return payload, []

    def decode_response(self, upstream_body: bytes, model: str, content_type: str) -> DecodeResult:
        try:
            payload = json.loads(upstream_body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            payload = None
        if not isinstance(payload, dict):
            raise UpstreamDecodeError("upstream body is not a JSON object")
        served_model = payload.get("model")
        if isinstance(payload.get("response"), dict):
            served_model = payload["response"].get("model", served_model)
        completion_chars = (
            chat_completion_chars(payload)
            if self._api == "chat"
            else responses_output_chars(payload.get("output"))
        )
        return DecodeResult(
            upstream_body,
            None,
            content_type,
            normalize_usage(payload),
            served_model if isinstance(served_model, str) else None,
            completion_chars,
        )

    def new_stream_translator(
        self, model: str, on_usage: Callable[[dict[str, int]], None]
    ) -> StreamTranslator:
        return _IdentityStreamTranslator(self._api, on_usage)


def select_adapter(upstream_api: str, request_path: str) -> Adapter:
    """Select the adapter for an upstream API and harness request path."""
    if upstream_api not in {"chat", "responses"}:
        raise ValueError(f"unsupported upstream API {upstream_api!r}")
    request_apis = {
        "/v1/chat/completions": "chat",
        "/v1/responses": "responses",
    }
    try:
        request_api = request_apis[request_path]
    except KeyError:
        raise ValueError(f"unsupported request path {request_path!r}") from None
    if upstream_api == request_api:
        return _IdentityAdapter(upstream_api)
    if upstream_api == "responses" and request_api == "chat":
        return ResponsesAdapter()
    raise UnsupportedAdapterPair(upstream_api, request_api)


__all__ = ["Adapter", "UnsupportedAdapterPair", "select_adapter"]
