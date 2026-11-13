import uuid

import pytest

from harbor_research_runner.records.proxy_control import (
    CLOSED_BODY,
    CloseMessage,
    OpenMessage,
    ProxyConfig,
    read,
    session_id_for_run,
    trial_base_url,
    trial_from_path,
    write,
)


def _config():
    return ProxyConfig(
        host="localhost",
        port=8766,
        ledger_path="ledger",
        bodies_dir=None,
        model={},
        limits={},
        restart_cooldown_s=2.0,
    )


def test_proxy_control_paths_and_closed_body():
    assert trial_base_url("localhost", 8766, "trial") == "http://localhost:8766/trial/trial/v1"
    assert trial_from_path("/trial/fix-git__Ab3dE9x/v1/chat/completions") == (
        "fix-git__Ab3dE9x",
        "/v1/chat/completions",
    )
    assert trial_from_path("/v1/chat/completions") is None
    for word in ("rate limit", "too many requests", "quota", "usage limit", "credits"):
        assert word not in CLOSED_BODY.lower()


@pytest.mark.parametrize(
    "record",
    [
        OpenMessage("trial", "task", "arm"),
        CloseMessage("trial"),
        _config(),
    ],
    ids=["open", "close", "config"],
)
def test_proxy_control_records_round_trip(record):
    assert type(record).from_row(record.to_row()) == record


@pytest.mark.parametrize(
    ("record", "field"),
    [
        (record, field)
        for record in (OpenMessage("trial", "task", "arm"), CloseMessage("trial"), _config())
        for field in record.to_row()
    ],
    ids=[
        f"{type(record).__name__}-{field}"
        for record in (OpenMessage("trial", "task", "arm"), CloseMessage("trial"), _config())
        for field in record.to_row()
    ],
)
def test_proxy_control_parser_rejects_missing_field(record, field):
    row = record.to_row()
    row.pop(field)

    with pytest.raises(ValueError):
        type(record).from_row(row)


@pytest.mark.parametrize(
    "record",
    [
        OpenMessage("trial", "task", "arm"),
        CloseMessage("trial"),
        _config(),
    ],
    ids=["open", "close", "config"],
)
def test_proxy_control_parser_rejects_unknown_field(record):
    row = record.to_row()
    row["extra"] = True

    with pytest.raises(ValueError):
        type(record).from_row(row)


def test_proxy_config_write_and_read(tmp_path):
    path = tmp_path / "proxy-config.json"
    config = _config()

    write(path, config)
    assert read(path) == config


def test_proxy_session_id_is_stable_for_a_run_folder():
    run_folder_name = "rehearsal-pilot-20261113T142740931165Z"
    first = session_id_for_run(run_folder_name)
    second = session_id_for_run(run_folder_name)

    assert first == second
    assert first == uuid.uuid5(uuid.NAMESPACE_URL, "hrr-run:" + run_folder_name).hex


def test_proxy_config_rejects_missing_required_session_id():
    with pytest.raises(ValueError, match="session_id"):
        ProxyConfig(
            host="localhost",
            port=8766,
            ledger_path="ledger",
            bodies_dir=None,
            model={"request": {"session_header": "x-opencode-session"}},
            limits={},
            restart_cooldown_s=0.0,
            session_id="",
        )
