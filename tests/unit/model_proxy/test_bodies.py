import gzip
import json
import logging
from concurrent.futures import ThreadPoolExecutor

from harbor_research_runner.model_proxy.bodies import BodyStore


def test_two_writes_are_compressed_and_round_trip(tmp_path):
    store = BodyStore(tmp_path)

    store.write("trial", "request-1", b'{"a":1}', b'{"b":2}')
    store.write("trial", "request-2", b'{"a":3}', b'{"b":4}')
    store.close_trial("trial")

    plain_path = tmp_path / "trial.jsonl"
    compressed_path = tmp_path / "trial.jsonl.gz"
    assert not plain_path.exists()
    assert compressed_path.exists()
    rows = [json.loads(line) for line in gzip.decompress(compressed_path.read_bytes()).splitlines()]
    assert rows == [
        {"request_id": "request-1", "request": '{"a":1}', "response": '{"b":2}'},
        {"request_id": "request-2", "request": '{"a":3}', "response": '{"b":4}'},
    ]


def test_none_directory_disables_writes():
    store = BodyStore(None)

    store.write("trial", "request", b"request", b"response")
    store.close_trial("trial")


def test_store_failure_logs_warning_and_does_not_raise(tmp_path, caplog):
    directory = tmp_path / "not-a-directory"
    directory.write_text("file", encoding="utf-8")
    store = BodyStore(directory)

    with caplog.at_level(logging.WARNING):
        store.write("trial", "request", b"request", b"response")

    assert any(record.levelno == logging.WARNING for record in caplog.records)


def test_invalid_trial_name_is_refused(tmp_path, caplog):
    store = BodyStore(tmp_path)

    with caplog.at_level(logging.WARNING):
        store.write("../x", "request", b"request", b"response")

    assert list(tmp_path.iterdir()) == []
    assert any(record.levelno == logging.WARNING for record in caplog.records)


def test_concurrent_writes_keep_each_line_intact(tmp_path):
    store = BodyStore(tmp_path)

    def write(index):
        body = (str(index) * 1000).encode()
        store.write("trial", f"request-{index}", body, body)

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(write, range(100)))
    store.close_trial("trial")

    lines = gzip.decompress((tmp_path / "trial.jsonl.gz").read_bytes()).splitlines()
    rows = [json.loads(line) for line in lines]
    assert len(rows) == 100
    assert {row["request_id"] for row in rows} == {f"request-{index}" for index in range(100)}


def test_write_after_close_does_not_recreate_plain_file(tmp_path, caplog):
    store = BodyStore(tmp_path)
    store.write("trial", "first", b"request", b"response")
    store.close_trial("trial")

    with caplog.at_level(logging.WARNING):
        store.write("trial", "late", b"request", b"response")

    assert not (tmp_path / "trial.jsonl").exists()
    assert any(record.levelno == logging.WARNING for record in caplog.records)


def test_compressed_output_is_deterministic_without_filename(tmp_path):
    outputs = []
    for index in range(2):
        directory = tmp_path / str(index)
        directory.mkdir()
        store = BodyStore(directory)
        store.write("trial", "request", b"request", b"response")
        store.close_trial("trial")
        outputs.append((directory / "trial.jsonl.gz").read_bytes())

    assert outputs[0] == outputs[1]
    assert outputs[0][3] & 0x08 == 0


def test_undecodable_body_bytes_are_replaced(tmp_path):
    store = BodyStore(tmp_path)
    store.write("trial", "request", b"a\xffb", b"c\xfed")

    row = json.loads((tmp_path / "trial.jsonl").read_text(encoding="utf-8"))

    assert row["request"] == "a\ufffdb"
    assert row["response"] == "c\ufffdd"
