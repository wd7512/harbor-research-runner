from __future__ import annotations

import threading
from collections.abc import Iterator, Mapping
from typing import TypedDict

from harbor_research_runner.model_proxy.relay import Route


class ModelCardView(TypedDict):
    endpoints: list[str]
    key_env: list[str]


class KeyProvider:
    def __init__(self, card: ModelCardView, environment: Mapping[str, str]) -> None:
        endpoints = card["endpoints"]
        key_names = card["key_env"]
        keys = []
        for name in key_names:
            if name not in environment:
                raise KeyError(name)
            keys.append(environment[name])
        route_keys: tuple[str | None, ...] = tuple(keys) if keys else (None,)
        self._routes = tuple(Route(endpoint, key) for endpoint in endpoints for key in route_keys)
        self._next_route = 0
        self._lock = threading.Lock()

    def routes(self) -> Iterator[Route]:
        if not self._routes:
            return iter(())
        with self._lock:
            start = self._next_route
            self._next_route = (start + 1) % len(self._routes)
        return iter(self._routes[start:] + self._routes[:start])
