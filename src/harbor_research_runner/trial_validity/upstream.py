import math

from harbor_research_runner.records.limits import Limits
from harbor_research_runner.records.proxy_ledger import COUNTS_AGAINST_TRIAL, parse_row


def _usable_ends(ledger_rows: list[dict]) -> list[dict]:
    ends = []
    for row in ledger_rows:
        if row.get("type") != "request_end":
            continue
        try:
            parse_row(row)
        except ValueError:
            continue
        ends.append(row)
    return ends


def upstream_reason(ledger_rows: list[dict], limits: Limits) -> str | None:
    """Rules 7, 8 and 9 over one trial's ledger rows, first match wins."""
    ends = _usable_ends(ledger_rows)
    if not ends:
        return None

    last = max(ends, key=lambda row: row["seq"])
    if last.get("upstream_status") in COUNTS_AGAINST_TRIAL:
        return "upstream_terminal_error"

    errors = sum(1 for row in ends if row.get("upstream_status") in COUNTS_AGAINST_TRIAL)
    requests = sum(1 for row in ends if row.get("upstream_status") != "client_disconnect")
    tolerated = max(
        limits.tolerated_errors,
        math.floor(limits.tolerated_error_fraction * requests),
    )
    if errors > tolerated:
        return "upstream_errors"

    ok_rows = [row for row in ends if row.get("upstream_status") == "ok" and row["duration_s"] > 0]
    if ok_rows and any((row.get("completion_tokens") or 0) > 0 for row in ok_rows):
        completion_tokens = sum(row.get("completion_tokens") or 0 for row in ok_rows)
        duration_s = sum(row["duration_s"] for row in ok_rows)
        if completion_tokens / duration_s < limits.trial_output_rate_min:
            return "upstream_slow"
    return None
