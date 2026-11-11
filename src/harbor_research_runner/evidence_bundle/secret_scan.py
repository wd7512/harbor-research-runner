"""Detect credentials in evidence bundle outputs."""

from __future__ import annotations

import gzip
import re
import zlib
from collections.abc import Mapping, Sequence

MIN_KEY_LENGTH = 8

PROVIDER_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("openai-style key", re.compile(r"sk-[A-Za-z0-9]{32,}")),
    ("github PAT", re.compile(r"ghp_[A-Za-z0-9]{36}")),
    ("github fine-grained PAT", re.compile(r"github_pat_[A-Za-z0-9_]{50,}")),
    ("aws access key id", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("google api key", re.compile(r"AIza[0-9A-Za-z_-]{35}")),
    ("slack token", re.compile(r"xox[baprs]-[0-9A-Za-z-]{10,}")),
    ("anthropic-style key", re.compile(r"sk-ant-[A-Za-z0-9_-]{24,}")),
    ("ahe gateway key", re.compile(r"ahe_[A-Za-z0-9]{24,}")),
    ("jwt", re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
    ("private key block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
)

_BYTE_PATTERNS: tuple[tuple[str, re.Pattern[bytes]], ...] = tuple(
    (label, re.compile(pattern.pattern.encode("ascii"))) for label, pattern in PROVIDER_PATTERNS
)


def key_values(model_card: dict, environment: Mapping[str, str]) -> tuple[str, ...]:
    """Return the card's set key values that are long enough to scan for."""
    found = set()
    for name in model_card["key_env"]:
        value = environment.get(name, "").strip()
        if len(value) >= MIN_KEY_LENGTH:
            found.add(value)
    return tuple(sorted(found))


def scan(files: Mapping[str, bytes], values: Sequence[str]) -> list[str]:
    """Return one secret-safe problem per credential match in the given files."""
    declared_text = sorted({value for value in values if len(value) >= MIN_KEY_LENGTH})
    declared = sorted({value.encode("utf-8") for value in declared_text})
    problems: list[str] = []
    for name in sorted(files):
        location = _location(name, declared_text)
        data = files[name]
        payloads = [data]
        if name.endswith(".gz"):
            try:
                decompressed = gzip.decompress(data)
            except (OSError, EOFError, zlib.error):
                problems.append(f"{location}: unreadable gzip")
            else:
                if decompressed != data:
                    payloads.append(decompressed)
        for payload in payloads:
            for value in declared:
                offset = payload.find(value)
                while offset >= 0:
                    problems.append(f"{location}: declared credential {_shown(value)}")
                    offset = payload.find(value, offset + 1)
            for label, pattern in _BYTE_PATTERNS:
                for match in pattern.finditer(payload):
                    problems.append(f"{location}: {label} {_shown(match.group())}")
    return problems


def _location(name: str, declared: Sequence[str]) -> str:
    if any("/" in value and value in name for value in declared):
        return "<credential>"
    parts = []
    for part in name.split("/"):
        if any(value in part for value in declared) or any(
            pattern.search(part) for _, pattern in PROVIDER_PATTERNS
        ):
            parts.append("<credential>")
        else:
            parts.append(
                "".join(char if char.isprintable() else f"\\u{ord(char):04x}" for char in part)
            )
    return "/".join(parts)


def _shown(match: bytes) -> str:
    return match[:4].decode("utf-8", errors="replace") + "..."
