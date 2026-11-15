"""Chat Completions <-> Responses API translation.

opencode speaks chat (`api: openai`); Responses-only models reject chat keys
(`messages`, `max_tokens`, chat-shaped `tools`) at the gateway. This adapter
translates the request upstream and the reply back to chat so the client
never sees Responses shapes. Pure functions; no I/O.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import Callable
from typing import Any

from harbor_research_runner.model_proxy.adapters.base import (
    DecodeResult,
    StreamTranslator,
    UpstreamAdapter,
    UpstreamDecodeError,
    normalize_usage,
)
from harbor_research_runner.model_proxy.chars import (
    responses_delta_chars,
    responses_output_chars,
    tool_call_chars,
)
from harbor_research_runner.model_proxy.usage import as_int

logger = logging.getLogger(__name__)


def _content_to_string(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    return json.dumps(content)


def _apply_tool_fields(out: dict[str, Any], source: dict[str, Any]) -> dict[str, Any]:
    if source.get("description") is not None:
        out["description"] = source["description"]
    if source.get("parameters") is not None:
        out["parameters"] = source["parameters"]
    return out


def _translate_tool(tool: Any) -> dict[str, Any]:
    if not isinstance(tool, dict):
        return {"type": "function", "name": "unknown", "parameters": {}}
    fn = tool.get("function")
    if isinstance(fn, dict):
        out: dict[str, Any] = {"type": "function", "name": str(fn.get("name", "unknown"))}
        return _apply_tool_fields(out, fn)
    out = {"type": "function", "name": str(tool.get("name", "unknown"))}
    return _apply_tool_fields(out, tool)


def _tool_result_item(message: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "function_call_output",
        "call_id": str(message.get("tool_call_id", "")),
        "output": _content_to_string(message.get("content")),
    }


def _assistant_tool_items(message: dict[str, Any], tool_calls: list[Any]) -> list[Any]:
    items: list[Any] = []
    text = _assistant_text(message.get("content"))
    if text:
        items.append({"role": "assistant", "content": text})
    for call in tool_calls:
        if not isinstance(call, dict):
            continue
        fn = call.get("function") or {}
        items.append(
            {
                "type": "function_call",
                "call_id": str(call.get("id", "")),
                "name": str(fn.get("name", "unknown")),
                "arguments": _args_to_string(fn.get("arguments", "")),
            }
        )
    return items


def _translate_messages(messages: Any) -> list[Any]:
    translated: list[Any] = []
    if not isinstance(messages, list):
        return translated
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        if role == "tool":
            translated.append(_tool_result_item(message))
            continue
        tool_calls = message.get("tool_calls")
        if role == "assistant" and isinstance(tool_calls, list) and tool_calls:
            translated.extend(_assistant_tool_items(message, tool_calls))
            continue
        if role in ("system", "developer", "user", "assistant"):
            translated.append({"role": role, "content": message.get("content")})
    return translated


def _assistant_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts = [
            part["text"]
            for part in content
            if isinstance(part, dict)
            and part.get("type") == "text"
            and isinstance(part.get("text"), str)
        ]
        if any(not isinstance(part, dict) or part.get("type") != "text" for part in content):
            logger.warning("dropping non-text assistant content parts")
        return "".join(texts)
    return ""


def _content_forward_warning(message: dict[str, Any]) -> str | None:
    content = message.get("content")
    if not isinstance(content, list):
        return None
    if message.get("role") in ("system", "developer", "user"):
        return f"message content for role {message.get('role')} forwarded verbatim"
    if not message.get("tool_calls"):
        return "assistant message content forwarded verbatim"
    return None


def translation_warnings(payload: dict[str, Any]) -> list[str]:
    warnings: list[str] = []
    dropped = sorted(key for key in payload if key not in _KNOWN_CHAT_KEYS)
    if dropped:
        warnings.append(f"dropping unsupported chat keys {dropped}")
    tool_choice = payload.get("tool_choice")
    if tool_choice is not None and not (
        isinstance(tool_choice, str) and tool_choice in _TOOL_CHOICE_ALLOWED
    ):
        warnings.append(f"dropping unsupported tool_choice {tool_choice!r}")
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return warnings
    for message in messages:
        if not isinstance(message, dict):
            continue
        warning = _content_forward_warning(message)
        if warning is not None:
            warnings.append(warning)
    return warnings


_TOOL_CHOICE_ALLOWED = frozenset({"auto", "none", "required"})


def _args_to_string(arguments: Any) -> str:
    if isinstance(arguments, str):
        return arguments
    if arguments is None:
        return ""
    return json.dumps(arguments)


_TERMINAL_OK = frozenset({"completed", "incomplete"})

_SUCCESS_TERMINAL_EVENTS = frozenset({"response.completed", "response.incomplete"})

_IGNORED_STREAM_EVENTS = frozenset(
    {
        "response.in_progress",
        "response.output_item.done",
        "response.output_text.done",
        "response.output_text.annotation.added",
        "response.output_text.annotation.done",
        "response.content_part.added",
        "response.content_part.done",
        "response.function_call_arguments.done",
        "response.reasoning_summary_part.added",
        "response.reasoning_summary_part.done",
        "response.reasoning_summary_text.delta",
        "response.reasoning_summary_text.done",
        "response.reasoning_text.delta",
        "response.reasoning_text.done",
        "response.refusal.delta",
        "response.refusal.done",
    }
)


def _stream_error_message(obj: dict[str, Any]) -> str:
    detail = obj.get("error")
    if isinstance(detail, dict):
        return str(detail.get("message") or detail)
    if detail:
        return str(detail)
    return "upstream stream error event"


def _check_status(status: Any) -> None:
    if status is not None and status not in _TERMINAL_OK:
        raise UpstreamDecodeError(f"upstream response status {status!r} is not usable")


_KNOWN_CHAT_KEYS = frozenset(
    {
        "model",
        "messages",
        "temperature",
        "top_p",
        "stream",
        "parallel_tool_calls",
        "max_output_tokens",
        "max_completion_tokens",
        "max_tokens",
        "tools",
        "tool_choice",
        "store",
        "reasoning_effort",
        "reasoning",
    }
)


def chat_to_responses(payload: dict[str, Any], model: str) -> dict[str, Any]:
    out: dict[str, Any] = {
        "model": model,
        "input": _translate_messages(payload.get("messages")),
    }
    for key in ("temperature", "top_p", "stream", "parallel_tool_calls", "store"):
        if key in payload:
            out[key] = payload[key]
    if payload.get("max_output_tokens") is not None:
        out["max_output_tokens"] = payload["max_output_tokens"]
    elif payload.get("max_completion_tokens") is not None:
        out["max_output_tokens"] = payload["max_completion_tokens"]
    elif payload.get("max_tokens") is not None:
        out["max_output_tokens"] = payload["max_tokens"]
    tools = payload.get("tools")
    if isinstance(tools, list):
        out["tools"] = [_translate_tool(tool) for tool in tools]
    tool_choice = payload.get("tool_choice")
    if isinstance(tool_choice, str) and tool_choice in _TOOL_CHOICE_ALLOWED:
        out["tool_choice"] = tool_choice
    reasoning = payload.get("reasoning")
    if isinstance(reasoning, dict):
        out["reasoning"] = reasoning
    if payload.get("reasoning_effort") is not None:
        out["reasoning"] = {"effort": payload["reasoning_effort"]}
    for warning in translation_warnings(payload):
        logger.warning("%s", warning)
    return out


def _message_text(item: Any) -> str:
    if not isinstance(item, dict) or item.get("type") != "message":
        return ""
    content = item.get("content") or []
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for part in content:
        if not isinstance(part, dict) or part.get("type") != "output_text":
            continue
        text = part.get("text")
        if isinstance(text, str):
            parts.append(text)
    return "".join(parts)


def _response_text(output: Any) -> str:
    if not isinstance(output, list):
        return ""
    return "".join(_message_text(item) for item in output)


def _as_usage_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_int(value: Any) -> int:
    return as_int(value)


def _chat_usage(usage: dict[str, Any], input_details: dict[str, Any]) -> dict[str, Any]:
    return {
        "prompt_tokens": _as_int(usage.get("input_tokens", 0)),
        "completion_tokens": _as_int(usage.get("output_tokens", 0)),
        "total_tokens": _as_int(usage.get("total_tokens", 0)),
        "prompt_tokens_details": {"cached_tokens": _as_int(input_details.get("cached_tokens", 0))},
    }


def _ledger_triple(usage: dict[str, Any], input_details: dict[str, Any]) -> dict[str, int]:
    return {
        "prompt_tokens": _as_int(usage.get("input_tokens", 0)),
        "completion_tokens": _as_int(usage.get("output_tokens", 0)),
        "cached_read_tokens": _as_int(input_details.get("cached_tokens", 0)),
    }


def _finish_reason(status: Any, has_tool_calls: bool) -> str:
    if has_tool_calls:
        return "tool_calls"
    if status == "incomplete":
        return "length"
    return "stop"


def _created_at(body: dict[str, Any]) -> int:
    created = body.get("created_at", body.get("created", 0))
    return created if isinstance(created, int) else 0


def _response_tool_calls(output: Any) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    if not isinstance(output, list):
        return calls
    for item in output:
        if not isinstance(item, dict) or item.get("type") != "function_call":
            continue
        calls.append(
            {
                "id": str(item.get("call_id", "")),
                "type": "function",
                "function": {
                    "name": str(item.get("name", "unknown")),
                    "arguments": _args_to_string(item.get("arguments", "")),
                },
            }
        )
    return calls


def responses_to_chat(body: dict[str, Any], model: str) -> dict[str, Any]:
    status = body.get("status")
    if status is not None and status not in _TERMINAL_OK:
        raw_usage = body.get("usage")
        failed_usage: dict[str, int] | None = None
        if isinstance(raw_usage, dict):
            usage_dict = _as_usage_dict(raw_usage)
            input_details = _as_usage_dict(usage_dict.get("input_tokens_details"))
            failed_usage = _ledger_triple(usage_dict, input_details)
        raise UpstreamDecodeError(
            f"upstream response status {status!r} is not usable",
            usage=failed_usage,
            status=status if isinstance(status, str) else None,
        )
    output = body.get("output")
    tool_calls = _response_tool_calls(output)
    usage = _as_usage_dict(body.get("usage"))
    input_details = _as_usage_dict(usage.get("input_tokens_details"))
    return {
        "id": body.get("id") or f"chatcmpl-{uuid.uuid4().hex}",
        "object": "chat.completion",
        "created": _created_at(body),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": _response_text(output),
                    **({"tool_calls": tool_calls} if tool_calls else {}),
                },
                "finish_reason": _finish_reason(body.get("status"), bool(tool_calls)),
            }
        ],
        "usage": _chat_usage(usage, input_details),
    }


class ResponsesStreamTranslator(StreamTranslator):
    """Incremental Responses SSE -> chat SSE translator.

    Unknown event types are ignored; when the completed event arrives with
    output never seen as deltas, the full text and tool calls are re-emitted
    before the finish chunk. Partially streamed text is not diffed. `usage`
    returns the normalized chat usage once the completed event arrives, else
    None. A stream ending without the completed event is truncation: check
    `finished` and fail the request rather than emitting success.
    """

    def __init__(
        self,
        *,
        model: str,
        response_id: str | None = None,
        on_usage: Callable[[dict[str, int]], None] | None = None,
    ):
        self._model = model
        self._chat_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
        self._created = int(time.time())
        self._functions: dict[int, dict[str, str]] = {}
        self._call_ordinals: dict[int, int] = {}
        self._usage: dict[str, Any] | None = None
        self._served_model: str | None = None
        self._finish_reason = "stop"
        self._streamed_tool_args: dict[int, bool] = {}
        self._streamed_text = False
        self._finished = False
        self._completion_chars = 0
        self._warned_events: set[str] = set()
        self._on_usage = on_usage
        if response_id:
            self._chat_id = response_id

    @property
    def usage(self) -> dict[str, Any] | None:
        return self._usage

    @property
    def finished(self) -> bool:
        return self._finished

    @property
    def completion_chars(self) -> int:
        return self._completion_chars

    @property
    def served_model(self) -> str | None:
        return self._served_model

    def _ordinal(self, upstream_index: int) -> int:
        if upstream_index not in self._call_ordinals:
            self._call_ordinals[upstream_index] = len(self._call_ordinals)
        return self._call_ordinals[upstream_index]

    def _chunk(self, delta: dict[str, Any]) -> str:
        return (
            "data: "
            + json.dumps(
                {
                    "id": self._chat_id,
                    "object": "chat.completion.chunk",
                    "created": self._created,
                    "model": self._model,
                    "choices": [{"index": 0, "delta": delta}],
                }
            )
            + "\n\n"
        )

    def feed_line(self, line: str) -> list[str]:
        if not line.startswith("data:"):
            return []
        raw = line[5:].strip()
        if raw in ("", "[DONE]"):
            return []
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            return []
        if not isinstance(obj, dict):
            return []
        return self.feed(obj)

    def _upstream_index(self, obj: dict[str, Any]) -> int:
        index = obj.get("output_index", 0)
        return index if isinstance(index, int) else 0

    def _on_created(self, obj: dict[str, Any]) -> list[str]:
        response = obj.get("response") or {}
        if not isinstance(response, dict):
            return []
        model = response.get("model")
        if isinstance(model, str):
            self._served_model = model
        if isinstance(response.get("id"), str):
            self._chat_id = response["id"]
        if isinstance(response.get("created_at"), int):
            self._created = response["created_at"]
        return []

    def _on_text_delta(self, obj: dict[str, Any]) -> list[str]:
        delta = obj.get("delta")
        if not isinstance(delta, str) or not delta:
            return []
        self._streamed_text = True
        self._completion_chars += responses_delta_chars(obj)
        return [self._chunk({"content": delta})]

    def _on_item_added(self, obj: dict[str, Any]) -> list[str]:
        item = obj.get("item") or {}
        if not isinstance(item, dict) or item.get("type") != "function_call":
            return []
        index = self._ordinal(self._upstream_index(obj))
        self._functions[index] = {
            "call_id": str(item.get("call_id", "")),
            "name": str(item.get("name", "unknown")),
        }
        return []

    def _on_tool_args_delta(self, obj: dict[str, Any]) -> list[str]:
        delta = obj.get("delta")
        if not isinstance(delta, str) or not delta:
            return []
        index = self._ordinal(self._upstream_index(obj))
        self._streamed_tool_args[index] = True
        self._completion_chars += responses_delta_chars(obj)
        fn = self._functions.get(index, {})
        return [
            self._chunk(
                {
                    "tool_calls": [
                        {
                            "index": index,
                            "id": fn.get("call_id") or None,
                            "type": "function",
                            "function": {
                                "name": fn.get("name", "unknown"),
                                "arguments": delta,
                            },
                        }
                    ]
                }
            )
        ]

    def feed(self, obj: dict[str, Any]) -> list[str]:
        model = obj.get("model")
        response = obj.get("response")
        if isinstance(response, dict) and isinstance(response.get("model"), str):
            model = response["model"]
        if isinstance(model, str):
            self._served_model = model
        event = obj.get("type", "")
        if event == "response.created":
            return self._on_created(obj)
        if event == "response.output_text.delta":
            return self._on_text_delta(obj)
        if event == "response.output_item.added":
            return self._on_item_added(obj)
        if event == "response.function_call_arguments.delta":
            return self._on_tool_args_delta(obj)
        if event in _SUCCESS_TERMINAL_EVENTS:
            return self._finish(obj.get("response") or {})
        if event == "response.failed":
            return self._fail(obj.get("response") or {})
        if event == "error":
            raise UpstreamDecodeError(_stream_error_message(obj))
        if event not in _IGNORED_STREAM_EVENTS and event and event not in self._warned_events:
            self._warned_events.add(event)
            logger.warning(
                "responses stream: unhandled event type %r; if it is "
                "terminal the stream will end unfinished and be recorded "
                "as truncated_stream",
                event,
            )
        return []

    def _fail(self, response: Any) -> list[str]:
        """A terminal `response.failed`: record what upstream measured, then fail.

        Without this branch the event fell through to `return []`, so the
        stream ended unfinished and was recorded as `truncated_stream` while
        any usage upstream had already reported on the failed response
        never reached the ledger -- the proxy then read lower than harbor
        for a request that really happened.
        """
        if isinstance(response, dict):
            self._record_finish_usage(response)
        self._finished = True
        status = response.get("status") if isinstance(response, dict) else None
        raise UpstreamDecodeError(f"upstream response failed (status={status!r})")

    def _record_finish_usage(self, response: dict[str, Any]) -> None:
        usage = _as_usage_dict(response.get("usage"))
        input_details = _as_usage_dict(usage.get("input_tokens_details"))
        self._usage = _chat_usage(usage, input_details)
        if self._on_usage is not None:
            triple = _ledger_triple(usage, input_details)
            triple["prompt_chars"] = 0
            triple["completion_chars"] = self._completion_chars
            self._on_usage(triple)
        if isinstance(response.get("created_at"), int):
            self._created = response["created_at"]

    def _unseen_tool_chunk(self, tool_calls: list[dict[str, Any]]) -> str | None:
        unseen = [
            (i, call) for i, call in enumerate(tool_calls) if i not in self._streamed_tool_args
        ]
        if not unseen:
            return None
        return self._chunk(
            {
                "tool_calls": [
                    {
                        "index": i,
                        "id": call["id"],
                        "type": "function",
                        "function": call["function"],
                    }
                    for i, call in unseen
                ]
            }
        )

    def _terminal_chunk(self) -> str:
        return (
            "data: "
            + json.dumps(
                {
                    "id": self._chat_id,
                    "object": "chat.completion.chunk",
                    "created": self._created,
                    "model": self._model,
                    "choices": [{"index": 0, "delta": {}, "finish_reason": self._finish_reason}],
                    **({"usage": self._usage} if self._usage else {}),
                }
            )
            + "\n\n"
        )

    def _finish(self, response: Any) -> list[str]:
        lines: list[str] = []
        if isinstance(response, dict):
            _check_status(response.get("status"))
        output = response.get("output") if isinstance(response, dict) else None
        tool_calls = _response_tool_calls(output)
        text = _response_text(output)
        if text and not self._streamed_text:
            self._completion_chars += len(text)
        for i, call in enumerate(tool_calls):
            if i not in self._streamed_tool_args:
                self._completion_chars += tool_call_chars(call)
        if isinstance(response, dict):
            self._record_finish_usage(response)
        self._finished = True
        if text and not self._streamed_text:
            lines.append(self._chunk({"content": text}))
        if tool_calls:
            self._finish_reason = "tool_calls"
            chunk = self._unseen_tool_chunk(tool_calls)
            if chunk is not None:
                lines.append(chunk)
        elif isinstance(response, dict) and response.get("status") == "incomplete":
            self._finish_reason = "length"
        lines.append(self._terminal_chunk())
        lines.append("data: [DONE]\n\n")
        return lines


class ResponsesAdapter(UpstreamAdapter):
    def build_request(
        self, payload: dict[str, Any], model: str
    ) -> tuple[dict[str, Any], list[str]]:
        if "messages" not in payload and "input" in payload:
            out = dict(payload)
            out["model"] = model
            return out, []
        return chat_to_responses(payload, model), translation_warnings(payload)

    def decode_response(self, upstream_body: bytes, model: str, content_type: str) -> DecodeResult:
        try:
            upstream = json.loads(upstream_body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise UpstreamDecodeError("upstream body is not JSON") from None
        if not isinstance(upstream, dict):
            raise UpstreamDecodeError("upstream body is not a JSON object") from None
        translated = responses_to_chat(upstream, model)
        return DecodeResult(
            json.dumps(translated).encode(),
            upstream_body,
            "application/json",
            normalize_usage(translated),
            upstream.get("model") if isinstance(upstream.get("model"), str) else None,
            responses_output_chars(upstream.get("output")),
        )

    def new_stream_translator(
        self, model: str, on_usage: Callable[[dict[str, int]], None]
    ) -> StreamTranslator:
        return ResponsesStreamTranslator(model=model, on_usage=on_usage)
