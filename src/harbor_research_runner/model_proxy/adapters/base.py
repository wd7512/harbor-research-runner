"""Adapter SPI: the dialect boundary between the proxy core and an upstream.

The core imports only this module. `UpstreamAdapter` turns an outbound
payload into upstream bytes and decodes a whole reply back into
`(client body, ledger body, usage)`; `StreamTranslator` does the same work
one transport line at a time for streaming replies. Adding an upstream is
subclassing these, never editing `server.py`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from harbor_research_runner.model_proxy import usage as _usage


def normalize_usage(obj: dict[str, Any]) -> dict[str, int] | None:
    """Canonical usage normalization (see harbor_research_runner.model_proxy.usage)."""
    return _usage.normalize_usage(obj)


@dataclass
class DecodeResult:
    """A decoded non-streaming reply.

    `client_body` is the bytes sent to the client and kept as
    `response.json`; `upstream_body` is the untouched upstream reply,
    kept as `upstream.json` for the ledger when there is a difference
    between the two dialects, and None when there is not.
    """

    client_body: bytes
    upstream_body: bytes | None
    content_type: str
    usage: dict[str, int] | None


class UpstreamDecodeError(Exception):
    """A reply the adapter could not decode; the caller reports it, never guesses."""

    def __init__(
        self,
        message: str,
        usage: dict[str, int] | None = None,
        status: str | None = None,
    ) -> None:
        super().__init__(message)
        self.usage = usage
        self.status = status


class StreamTranslator:
    """Incremental decoder for one streaming reply.

    Fed the transport's lines as they arrive, it returns the lines the
    client should see. `finished` and `usage` become meaningful only once
    the upstream's terminator has been read.
    """

    def feed_line(self, line: str) -> list[str]:
        """Decode one transport line into zero or more client-visible lines."""
        raise NotImplementedError

    @property
    def finished(self) -> bool:
        """True once the upstream terminator has been seen and no line is buffered."""
        raise NotImplementedError

    @property
    def completion_chars(self) -> int:
        """Completion characters streamed so far, used for counts only."""
        return 0

    @property
    def usage(self) -> dict[str, Any] | None:
        """Usage as reported by the stream, or None when the upstream reported none."""
        return None


class UpstreamAdapter:
    """One upstream's request/reply dialect; all three methods are mandatory."""

    def build_request(
        self, payload: dict[str, Any], model: str
    ) -> tuple[dict[str, Any], list[str]]:
        """Outbound request body, plus the translation warnings the ledger should keep."""
        raise NotImplementedError

    def decode_response(self, upstream_body: bytes, model: str, content_type: str) -> DecodeResult:
        """Whole-reply decoding, used when the reply is not streamed."""
        raise NotImplementedError

    def new_stream_translator(
        self, model: str, on_usage: Callable[[dict[str, int]], None]
    ) -> StreamTranslator:
        """A translator for exactly one streaming reply; `on_usage` fires when usage lands."""
        raise NotImplementedError
