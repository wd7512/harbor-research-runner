The proxy now checks trial admission and records `request_start` under the same lock after pacing, so a request waiting when its trial closes is refused before reaching upstream.
