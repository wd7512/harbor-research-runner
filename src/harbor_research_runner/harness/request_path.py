_REQUEST_PATHS = {
    "opencode": "/v1/chat/completions",
    "fake": "/v1/chat/completions",
}


def request_path(harness: str) -> str:
    try:
        return _REQUEST_PATHS[harness]
    except KeyError:
        raise ValueError(f"unknown harness {harness!r}") from None
