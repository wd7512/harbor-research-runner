def memory_watch_stats(
    rows: list[dict],
    *,
    start: float | None = None,
    end: float | None = None,
    declared: bool = False,
) -> dict[tuple[str, str], tuple[int | float | None, int | None]]:
    peaks = {}
    limits = {}
    for row in rows:
        trial = row.get("trial")
        location = row.get("location")
        if not isinstance(trial, str) or location not in {"harbor-job", "canary-job"}:
            continue
        if start is not None or end is not None:
            timestamp = row.get("t")
            if not isinstance(timestamp, (int, float)) or isinstance(timestamp, bool):
                continue
            if start is not None and timestamp < start:
                continue
            if end is not None and timestamp > end:
                continue
        key = (location, trial)
        limit_mb = row.get("limit_mb")
        if declared:
            limit_mb = row.get("declared_limit_mb", limit_mb)
        if isinstance(limit_mb, int) or limit_mb is None:
            limits[key] = limit_mb
        if row.get("type") != "sample":
            continue
        used_mb = row.get("used_mb")
        if isinstance(used_mb, (int, float)) and not isinstance(used_mb, bool):
            peaks[key] = max(peaks.get(key, float("-inf")), used_mb)
    return {key: (peaks.get(key), limits.get(key)) for key in peaks.keys() | limits.keys()}
