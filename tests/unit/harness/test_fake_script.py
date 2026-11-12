from __future__ import annotations

from pathlib import Path

import pytest

from harbor_research_runner.harness.fake_script import main, parse_args


def test_parse_args_uses_the_pinned_defaults() -> None:
    arguments = parse_args(["--base-url", "http://127.0.0.1:8766/trial/x/v1"])

    assert arguments.requests == 1
    assert arguments.exit_code == 0
    assert arguments.sleep_s == 0
    assert arguments.ignore_stop is False
    assert arguments.keep_requesting_s == 0
    assert arguments.solve is False
    assert arguments.out == Path("/app/hello.txt")


def test_parse_args_accepts_every_flag() -> None:
    arguments = parse_args(
        [
            "--base-url",
            "http://127.0.0.1:8766/trial/x/v1",
            "--requests",
            "4",
            "--exit-code",
            "3",
            "--sleep-s",
            "2.5",
            "--ignore-stop",
            "--keep-requesting-s",
            "1.5",
            "--solve",
            "--out",
            "/tmp/hello.txt",
        ]
    )

    assert arguments.requests == 4
    assert arguments.exit_code == 3
    assert arguments.sleep_s == 2.5
    assert arguments.ignore_stop is True
    assert arguments.keep_requesting_s == 1.5
    assert arguments.solve is True
    assert arguments.out == Path("/tmp/hello.txt")


def test_parse_args_requires_the_base_url() -> None:
    with pytest.raises(SystemExit):
        parse_args([])


def test_parse_args_rejects_an_unknown_flag() -> None:
    with pytest.raises(SystemExit):
        parse_args(["--base-url", "http://127.0.0.1:8766/v1", "--bogus"])


def test_main_returns_the_requested_exit_code_without_requests() -> None:
    exit_code = main(["--base-url", "http://127.0.0.1:1/v1", "--requests", "0", "--exit-code", "7"])

    assert exit_code == 7


def test_main_writes_hello_to_the_overridden_output(tmp_path: Path) -> None:
    output = tmp_path / "hello.txt"

    exit_code = main(
        [
            "--base-url",
            "http://127.0.0.1:1/v1",
            "--requests",
            "0",
            "--solve",
            "--out",
            str(output),
        ]
    )

    assert exit_code == 0
    assert output.read_bytes() == b"hello\n"
