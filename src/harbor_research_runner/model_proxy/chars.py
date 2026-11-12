"""Per-request character counts for cross-model verbosity comparison."""

from __future__ import annotations

import json
from typing import Any


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


def prompt_chars_of_payload(payload: Any) -> int:
    """Prompt characters in an outbound request payload."""
    if not isinstance(payload, dict):
        return 0
    total = 0
    messages = payload.get("messages")
    if isinstance(messages, list):
        for message in messages:
            if isinstance(message, dict):
                total += _assistant_message_chars(message)
    inputs = payload.get("input")
    if isinstance(inputs, list):
        for item in inputs:
            if isinstance(item, dict):
                if item.get("type") == "function_call":
                    total += tool_call_chars(item)
                elif item.get("type") == "function_call_output":
                    total += _content_chars(item.get("output"))
                else:
                    total += _content_chars(item.get("content"))
    tools = payload.get("tools")
    if tools:
        total += len(json.dumps(tools, default=str))
    return total


def tool_call_chars(call: Any) -> int:
    """Name and argument characters in one chat-shaped tool call."""
    if not isinstance(call, dict):
        return 0
    fn = call.get("function") if isinstance(call.get("function"), dict) else call
    if not isinstance(fn, dict):
        return 0
    total = 0
    name = fn.get("name")
    if isinstance(name, str):
        total += len(name)
    args = fn.get("arguments")
    if isinstance(args, str):
        total += len(args)
    elif args is not None:
        total += len(json.dumps(args, default=str))
    return total


def _assistant_message_chars(message: Any) -> int:
    if not isinstance(message, dict):
        return 0
    total = _content_chars(message.get("content"))
    tool_calls = message.get("tool_calls")
    if isinstance(tool_calls, list):
        for call in tool_calls:
            total += tool_call_chars(call)
    return total


def chat_completion_chars(body: Any) -> int:
    """Completion characters in a chat-shaped response body."""
    if not isinstance(body, dict):
        return 0
    total = 0
    choices = body.get("choices")
    if isinstance(choices, list):
        for choice in choices:
            if isinstance(choice, dict):
                total += _assistant_message_chars(choice.get("message"))
                total += _assistant_message_chars(choice.get("delta"))
    return total


def chat_delta_chars(obj: Any) -> int:
    """Completion characters in one chat stream chunk object."""
    if not isinstance(obj, dict):
        return 0
    total = 0
    choices = obj.get("choices")
    if isinstance(choices, list):
        for choice in choices:
            if isinstance(choice, dict):
                total += _assistant_message_chars(choice.get("delta"))
    return total


def _responses_text_of_content(content: Any) -> int:
    if not isinstance(content, list):
        return 0
    total = 0
    for part in content:
        if (
            isinstance(part, dict)
            and part.get("type") == "output_text"
            and isinstance(part.get("text"), str)
        ):
            total += len(part["text"])
    return total


def _responses_function_call_chars(item: Any) -> int:
    if not isinstance(item, dict) or item.get("type") != "function_call":
        return 0
    total = 0
    if isinstance(item.get("name"), str):
        total += len(item["name"])
    args = item.get("arguments")
    if isinstance(args, str):
        total += len(args)
    elif args is not None:
        total += len(json.dumps(args, default=str))
    return total


def responses_output_chars(output: Any) -> int:
    """Completion characters in a Responses output list."""
    if not isinstance(output, list):
        return 0
    total = 0
    for item in output:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "message":
            total += _responses_text_of_content(item.get("content"))
        else:
            total += _responses_function_call_chars(item)
    return total


def responses_delta_chars(obj: Any) -> int:
    """Completion characters in one Responses stream event object."""
    if not isinstance(obj, dict):
        return 0
    event = obj.get("type", "")
    if event in ("response.output_text.delta", "response.function_call_arguments.delta"):
        delta = obj.get("delta")
        return len(delta) if isinstance(delta, str) else 0
    return 0
