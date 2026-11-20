from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import httpx

from harbor_research_runner.evidence_bundle.build import build
from harbor_research_runner.model_proxy.adapters.responses import ResponsesAdapter
from harbor_research_runner.model_proxy.ledger import LedgerWriter
from harbor_research_runner.model_proxy.relay import RelayDeps, Route, relay
from harbor_research_runner.records.jsonl import read_rows
from harbor_research_runner.records.limits import Limits
from tests.support.fake_upstream import FakeUpstream, RawResponse

FIXTURES = Path(__file__).parents[1] / "fixtures" / "run_folders"


def test_build_excludes_unknown_prompt_usage_from_cost(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    shutil.copytree(FIXTURES / "clean", run_dir)
    metadata_path = run_dir / "run-metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["machine_check_sha256"] = hashlib.sha256(
        (run_dir / "machine-check.json").read_bytes()
    ).hexdigest()
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    response = RawResponse(
        [
            (
                b"event: response.failed\r\ndata: "
                b'{"type":"response.failed","response":{"status":"failed",'
                b'"output":[],"usage":{"input_tokens_details":{"cached_tokens":500},'
                b'"output_tokens":10}}}\r\n\r\n'
            )
        ]
    )
    upstream = FakeUpstream([response], model="served-model")
    base_url = upstream.start()
    ledger = LedgerWriter(tmp_path / "proxy-ledger.jsonl")
    client = httpx.Client(trust_env=False)
    deps = RelayDeps(
        ledger=ledger,
        model={"id": "card/model", "upstream_model": "served-model", "api": "responses"},
        limits=Limits.from_dict({}, repetitions=1, verifier_timeout_multiplier=1.0),
        client=client,
        routes=lambda: iter([Route(f"{base_url}/v1/responses", "test-key")]),
    )
    try:
        proxy_response = relay(
            json.dumps(
                {
                    "model": "card/model",
                    "stream": True,
                    "messages": [{"role": "user", "content": "hello"}],
                }
            ).encode(),
            {},
            "proxy-trial",
            deps,
            ResponsesAdapter(),
        )
        list(proxy_response.chunks)
    finally:
        ledger.close()
        client.close()
        upstream.stop()

    proxy_end = read_rows(tmp_path / "proxy-ledger.jsonl").rows[-1]
    assert proxy_end["completion_tokens"] == 10
    assert proxy_end["cached_read_tokens"] == 500

    fixture_ledger_path = run_dir / "proxy-ledger.jsonl"
    fixture_ledger = read_rows(fixture_ledger_path).rows
    trial = next(
        row["trial"]
        for row in fixture_ledger
        if row.get("type") == "request_end" and row["upstream_status"] == "ok"
    )
    end = next(
        row
        for row in fixture_ledger
        if row.get("type") == "request_end" and row.get("trial") == trial
    )
    for key in (
        "prompt_tokens",
        "completion_tokens",
        "cached_read_tokens",
        "completion_chars",
    ):
        end[key] = proxy_end[key]
    end["prompt_tokens"] = None
    end["upstream_status"] = "server_error"
    fixture_ledger_path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in fixture_ledger),
        encoding="utf-8",
    )

    output = tmp_path / "evidence"
    assert build(run_dir, output, {}) == 0
    trial_row = next(
        json.loads(line)
        for line in (output / "trials.jsonl").read_text(encoding="utf-8").splitlines()
        if json.loads(line)["trial"] == trial
    )
    assert trial_row["requests"] == 1
    assert trial_row["cost_usd_estimate"] == 0.0
