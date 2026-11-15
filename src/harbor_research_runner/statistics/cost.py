def cost_usd(
    pricing: dict,
    prompt_tokens: int,
    completion_tokens: int,
    cached_read_tokens: int,
) -> float:
    if cached_read_tokens > prompt_tokens:
        raise ValueError("cached_read_tokens cannot exceed prompt_tokens")
    return (
        (prompt_tokens - cached_read_tokens) * pricing["price_in"]
        + cached_read_tokens * pricing["price_cache"]
        + completion_tokens * pricing["price_out"]
    ) / 1_000_000
