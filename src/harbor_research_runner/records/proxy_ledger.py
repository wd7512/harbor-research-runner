from dataclasses import dataclass

UPSTREAM_STATUSES = (
    "ok",
    "rate_limited",
    "quota_exhausted",
    "server_error",
    "stalled",
    "truncated",
    "transport_error",
    "client_error",
    "client_disconnect",
)

COUNTS_AGAINST_TRIAL = frozenset(
    {
        "rate_limited",
        "quota_exhausted",
        "server_error",
        "stalled",
        "truncated",
        "transport_error",
    }
)


@dataclass(frozen=True)
class ProxyStart:
    seq: int
    t: float
    proxy_version: str
    config_sha256: str
    restart: bool

    def to_row(self) -> dict:
        return {
            "type": "proxy_start",
            "seq": self.seq,
            "t": self.t,
            "proxy_version": self.proxy_version,
            "config_sha256": self.config_sha256,
            "restart": self.restart,
        }


@dataclass(frozen=True)
class Open:
    seq: int
    t: float
    trial: str
    task: str
    arm: str

    def to_row(self) -> dict:
        return {
            "type": "open",
            "seq": self.seq,
            "t": self.t,
            "trial": self.trial,
            "task": self.task,
            "arm": self.arm,
        }


@dataclass(frozen=True)
class RequestStart:
    seq: int
    t: float
    trial: str
    request_id: str
    model: str
    is_stream: bool
    prompt_chars: int
    paced_wait_s: float

    def to_row(self) -> dict:
        return {
            "type": "request_start",
            "seq": self.seq,
            "t": self.t,
            "trial": self.trial,
            "request_id": self.request_id,
            "model": self.model,
            "is_stream": self.is_stream,
            "prompt_chars": self.prompt_chars,
            "paced_wait_s": self.paced_wait_s,
        }


@dataclass(frozen=True)
class RequestEnd:
    seq: int
    t: float
    trial: str
    request_id: str
    http_status: int
    upstream_status: str
    upstream_http_status: int | None
    served_model: str | None
    prompt_tokens: int | None
    completion_tokens: int | None
    cached_read_tokens: int | None
    completion_chars: int
    first_chunk_s: float | None
    first_content_s: float | None
    duration_s: float
    failovers: int
    error_excerpt: str | None

    def to_row(self) -> dict:
        return {
            "type": "request_end",
            "seq": self.seq,
            "t": self.t,
            "trial": self.trial,
            "request_id": self.request_id,
            "http_status": self.http_status,
            "upstream_status": self.upstream_status,
            "upstream_http_status": self.upstream_http_status,
            "served_model": self.served_model,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "cached_read_tokens": self.cached_read_tokens,
            "completion_chars": self.completion_chars,
            "first_chunk_s": self.first_chunk_s,
            "first_content_s": self.first_content_s,
            "duration_s": self.duration_s,
            "failovers": self.failovers,
            "error_excerpt": self.error_excerpt,
        }


@dataclass(frozen=True)
class Refused:
    seq: int
    t: float
    trial: str
    reason: str

    def to_row(self) -> dict:
        return {
            "type": "refused",
            "seq": self.seq,
            "t": self.t,
            "trial": self.trial,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class Close:
    seq: int
    t: float
    trial: str

    def to_row(self) -> dict:
        return {"type": "close", "seq": self.seq, "t": self.t, "trial": self.trial}


_ROW_TYPES = {
    "proxy_start": ProxyStart,
    "open": Open,
    "request_start": RequestStart,
    "request_end": RequestEnd,
    "refused": Refused,
    "close": Close,
}


def parse_row(row: dict) -> ProxyStart | Open | RequestStart | RequestEnd | Refused | Close:
    try:
        row_type = row["type"]
        record_type = _ROW_TYPES[row_type]
    except (KeyError, TypeError) as exc:
        raise ValueError("unknown proxy ledger row type") from exc

    fields = record_type.__dataclass_fields__
    names = tuple(fields)
    missing = [name for name in names if name not in row]
    if missing:
        raise ValueError(f"missing field: {missing[0]}")
    if record_type is RequestEnd and row["upstream_status"] not in UPSTREAM_STATUSES:
        raise ValueError("unknown upstream status")
    try:
        return record_type(**{name: row[name] for name in names})
    except TypeError as exc:
        raise ValueError("invalid proxy ledger row") from exc


def sequence_gaps(rows: list[dict]) -> list[tuple[int, int]]:
    return [
        (previous, current)
        for previous, current in zip(
            (row["seq"] for row in rows), (row["seq"] for row in rows[1:]), strict=False
        )
        if current != previous + 1
    ]


def rows_for_trial(rows: list[dict], trial: str) -> list[dict]:
    return [row for row in rows if row.get("trial") == trial]
