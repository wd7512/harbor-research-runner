from __future__ import annotations

import json

import pytest

from harbor_research_runner.harness.opencode_eras import LEGACY_V1_0, V2, HarnessFacts, config_files
from harbor_research_runner.harness.request_path import request_path


@pytest.mark.parametrize("harness", ["opencode", "fake"])
def test_request_path_uses_chat_for_shipped_harnesses(harness: str) -> None:
    assert request_path(harness) == "/v1/chat/completions"


def test_request_path_rejects_unknown_harness() -> None:
    with pytest.raises(ValueError, match="unknown harness"):
        request_path("unknown")


def test_opencode_request_path_matches_both_rendered_provider_eras() -> None:
    proxy_url = "http://host.docker.internal:8766/trial/trial-a/v1"
    eras = [
        HarnessFacts(version="1.0.19", help_text="", era=LEGACY_V1_0),
        HarnessFacts(version="2.0.32", help_text="", era=V2),
    ]

    assert request_path("opencode") == "/v1/chat/completions"
    legacy = json.loads(
        next(iter(config_files(eras[0], "provider/model", proxy_url, None).values()))
    )
    modern = json.loads(
        next(iter(config_files(eras[1], "provider/model", proxy_url, None).values()))
    )

    assert legacy["provider"]["provider"]["options"]["npm"] == "@ai-sdk/openai-compatible"
    assert legacy["provider"]["provider"]["options"]["baseURL"] == proxy_url
    assert modern["providers"]["provider"]["package"] == "@opencode/ai/providers/openai-compatible"
    assert modern["providers"]["provider"]["settings"]["baseURL"] == proxy_url
