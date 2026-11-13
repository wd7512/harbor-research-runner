from dataclasses import dataclass

from harbor_research_runner.records.limits import Limits, host_breach

SAMPLER_STALE_S = 40.0


@dataclass(frozen=True)
class RunSnapshot:
    now: float
    ledger_rows_since_pass_start: list[dict]
    verdicts: list[dict]
    open_trials: int
    proxy_alive: bool
    oom_watch_alive: bool
    resource_rows: list[dict]
    last_activity_t: float
    longest_agent_budget_s: float
    longest_verifier_budget_s: float
    harbor_timeout_multiplier: float
    stop_launching_at: float | None = None
    in_flight_trials: int = 0


def inactivity_limit_s(snapshot: RunSnapshot, limits: Limits) -> float:
    """The longest a run may go without ledger or result activity before halting."""
    return (
        snapshot.longest_agent_budget_s * snapshot.harbor_timeout_multiplier
        + snapshot.longest_verifier_budget_s * limits.verifier_timeout_multiplier
        + limits.inactivity_margin_s
    )


def _time(row: dict) -> float | None:
    value = row.get("t")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _zero_billed_anchor(snapshot: RunSnapshot, rows: list[dict]) -> float:
    ok_times = [
        time
        for row in rows
        if row.get("type") == "request_end"
        and row.get("upstream_status") == "ok"
        and (time := _time(row)) is not None
    ]
    anchor = max(ok_times) if ok_times else None
    if rows:
        first = _time(rows[0])
        if first is not None:
            anchor = first if anchor is None else max(anchor, first)
    return snapshot.last_activity_t if anchor is None else anchor


def _samples(snapshot: RunSnapshot) -> list[dict]:
    samples = [
        row
        for row in snapshot.resource_rows
        if row.get("type") == "sample" and _time(row) is not None
    ]
    return sorted(samples, key=lambda row: row["t"])


def _breach_suffix_start(samples: list[dict], limits: Limits) -> float | None:
    start = None
    for sample in reversed(samples):
        if not host_breach(sample, limits):
            break
        start = float(sample["t"])
    return start


def should_halt(
    snapshot: RunSnapshot, limits: Limits, poll_s: float = 5.0
) -> tuple[str, str] | None:
    """Return the first halt reason and detail the snapshot supports, or None."""
    rows = snapshot.ledger_rows_since_pass_start

    quota_rows = [
        row
        for row in rows
        if row.get("type") == "request_end" and row.get("upstream_status") == "quota_exhausted"
    ]
    if quota_rows:
        return (
            "quota_exhausted",
            f"{len(quota_rows)} quota-exhausted request(s) since pass start",
        )

    streak = limits.halt_after_consecutive_invalid
    if streak > 0 and len(snapshot.verdicts) >= streak:
        tail = snapshot.verdicts[-streak:]
        if all(row.get("instrument") == "proxy" and row.get("valid") is False for row in tail):
            return (
                "upstream_invalid_streak",
                f"{streak} consecutive proxy-invalid trial verdicts",
            )

    if snapshot.open_trials >= 1:
        anchor = _zero_billed_anchor(snapshot, rows)
        elapsed = snapshot.now - anchor
        if elapsed >= limits.zero_billed_window_s:
            return (
                "zero_billed_window",
                f"no billed request for {elapsed:.0f}s with {snapshot.open_trials} trial(s) open",
            )

    if not snapshot.proxy_alive:
        return ("proxy_dead", "proxy process exited")

    if not snapshot.oom_watch_alive:
        return ("oom_watch_dead", "OOM watcher process exited")

    samples = _samples(snapshot)
    if not samples:
        return ("sampler_dead", "no resource sample recorded")
    latest = float(samples[-1]["t"])
    age = snapshot.now - latest
    if age > SAMPLER_STALE_S:
        return ("sampler_dead", f"latest resource sample is {age:.0f}s old")

    breach_start = _breach_suffix_start(samples, limits)
    if breach_start is not None and latest - breach_start >= limits.host_breach_halt_s:
        return (
            "host_breach",
            f"every resource sample breached a host limit for {latest - breach_start:.0f}s",
        )

    idle = snapshot.now - snapshot.last_activity_t
    limit = inactivity_limit_s(snapshot, limits)
    if idle > limit:
        return ("inactive", f"no activity for {idle:.0f}s, limit {limit:.0f}s")
    if (
        snapshot.stop_launching_at is not None
        and snapshot.now >= snapshot.stop_launching_at + 2 * poll_s
        and snapshot.in_flight_trials == 0
    ):
        return (
            "deadline",
            f"launch deadline passed at {snapshot.stop_launching_at:.3f} with no in-flight trials",
        )
    return None
