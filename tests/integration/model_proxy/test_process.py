from __future__ import annotations

import http.client
import json
import signal
import threading
import urllib.error
import urllib.request

import pytest

from harbor_research_runner.records.jsonl import read_rows


def _get(url: str) -> tuple[int, bytes]:
    try:
        with urllib.request.urlopen(url, timeout=1) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


def _post(url: str, body: dict) -> tuple[int, bytes]:
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=1) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


def _trial_url(proxy, trial: str, path: str = "/v1/chat/completions") -> str:
    return f"{proxy.url}/trial/{trial}{path}"


def _request(model: str = "fake/model") -> dict:
    return {
        "model": model,
        "stream": True,
        "messages": [{"role": "user", "content": "hello"}],
    }


def _rows(proxy):
    return read_rows(proxy.ledger_path).rows


def test_health_answers_after_start(proxy):
    status, body = _get(f"{proxy.url}/control/health")

    assert status == 200
    assert json.loads(body) == {"ok": True, "seq": 1}


def test_unopened_trial_is_refused(proxy):
    status, _body = _post(_trial_url(proxy, "unopened"), _request())

    assert status == 410
    assert _rows(proxy)[-1]["type"] == "refused"
    assert _rows(proxy)[-1]["reason"] == "not_open"


def test_open_request_close_writes_ordered_rows(proxy):
    assert (
        _post(f"{proxy.url}/control/open", {"trial": "trial-1", "task": "task-1", "arm": "arm-1"})[
            0
        ]
        == 200
    )
    assert _post(_trial_url(proxy, "trial-1"), _request())[0] == 200
    assert _post(f"{proxy.url}/control/close", {"trial": "trial-1"})[0] == 200

    rows = _rows(proxy)
    assert [row["type"] for row in rows] == [
        "proxy_start",
        "open",
        "request_start",
        "request_end",
        "close",
    ]
    assert [row["seq"] for row in rows] == [1, 2, 3, 4, 5]
    assert (proxy.ledger_path.parent / "request-bodies" / "trial-1.jsonl.gz").is_file()
    assert not (proxy.ledger_path.parent / "request-bodies" / "trial-1.jsonl").exists()


def test_closed_trial_request_is_refused(proxy):
    _post(f"{proxy.url}/control/open", {"trial": "trial-1", "task": "task-1", "arm": "arm-1"})
    _post(f"{proxy.url}/control/close", {"trial": "trial-1"})

    status, _body = _post(_trial_url(proxy, "trial-1"), _request())

    assert status == 410
    assert _rows(proxy)[-1]["type"] == "refused"
    assert _rows(proxy)[-1]["reason"] == "closed"


def test_unknown_path_is_not_recorded(proxy):
    before = _rows(proxy)

    status, _body = _post(_trial_url(proxy, "trial-1", "/v1/unknown"), _request())

    assert status == 404
    assert _rows(proxy) == before


def test_unsupported_adapter_pair_is_refused_without_ledger_row(proxy):
    before = _rows(proxy)

    status, body = _post(_trial_url(proxy, "trial-1", "/v1/responses"), _request())

    assert status == 400
    assert json.loads(body) == {
        "error": {"message": "unsupported API for this model", "type": "unsupported_api"}
    }
    assert _rows(proxy) == before
    assert proxy.upstream.requests == []
    log = (proxy.ledger_path.parent / "proxy.log").read_text(encoding="utf-8")
    assert "trial-1" in log
    assert "/v1/responses" in log
    assert "api=chat" in log


def test_restart_continues_sequence_and_refuses_closed_trial(proxy):
    _post(f"{proxy.url}/control/open", {"trial": "trial-1", "task": "task-1", "arm": "arm-1"})
    _post(f"{proxy.url}/control/close", {"trial": "trial-1"})
    assert proxy.stop() == 0
    proxy.restart()

    status, _body = _post(_trial_url(proxy, "trial-1"), _request())

    rows = _rows(proxy)
    assert status == 410
    assert rows[-2]["type"] == "proxy_start"
    assert rows[-2]["restart"] is True
    assert [row["seq"] for row in rows] == [1, 2, 3, 4, 5]
    assert rows[-1]["type"] == "refused"
    assert rows[-1]["reason"] == "closed"


@pytest.mark.parametrize("proxy", [{"file_size_limit": 256}], indirect=True)
def test_ledger_write_failure_stops_serving(proxy):
    _post(f"{proxy.url}/control/open", {"trial": "trial-1", "task": "task-1", "arm": "arm-1"})

    status, body = _post(_trial_url(proxy, "trial-1"), _request("m" * 1000))
    code = proxy.process.wait(timeout=2)

    assert status == 503
    assert body == b"proxy ledger unavailable"
    assert code == 70


@pytest.mark.parametrize("proxy", [{"script": ["slow:5"]}], indirect=True)
def test_sigterm_waits_for_stream_to_finish(proxy):
    opened = _post(
        f"{proxy.url}/control/open", {"trial": "trial-1", "task": "task-1", "arm": "arm-1"}
    )
    assert opened[0] == 200
    headers_received = threading.Event()
    result: dict[str, object] = {}

    def request() -> None:
        connection = http.client.HTTPConnection(
            "127.0.0.1", int(proxy.url.rsplit(":", 1)[1]), timeout=5
        )
        connection.request(
            "POST",
            "/trial/trial-1/v1/chat/completions",
            body=json.dumps(_request()).encode(),
            headers={"Content-Type": "application/json"},
        )
        response = connection.getresponse()
        headers_received.set()
        result["status"] = response.status
        result["body"] = response.read()
        connection.close()

    thread = threading.Thread(target=request, daemon=True)
    thread.start()
    assert headers_received.wait(timeout=1.5)
    proxy.process.send_signal(signal.SIGTERM)
    thread.join(timeout=4.5)
    code = proxy.process.wait(timeout=0.5)

    assert not thread.is_alive()
    assert code == 0
    assert result["status"] == 200
    assert b"data: [DONE]" in result["body"]
