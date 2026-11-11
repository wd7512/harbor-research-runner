from __future__ import annotations

import gzip
import io

import pytest

from harbor_research_runner.evidence_bundle.secret_scan import key_values, scan

VALUE = "d7" * 32

PATTERN_CASES = [
    (
        "openai-style key",
        "sk-" + "a" * 40,
    ),
    (
        "github PAT",
        "ghp_" + "b" * 36,
    ),
    (
        "github fine-grained PAT",
        "github_pat_" + "c" * 50,
    ),
    (
        "aws access key id",
        "AKIA" + "D" * 16,
    ),
    (
        "google api key",
        "AIza" + "e" * 35,
    ),
    (
        "slack token",
        "xoxb-" + "f" * 12,
    ),
    (
        "anthropic-style key",
        "sk-ant-" + "g" * 24,
    ),
    (
        "ahe gateway key",
        "ahe_" + "h" * 24,
    ),
    (
        "jwt",
        "eyJhbGciOiJIUzI1NiJ9."
        + "eyJzdWIiOiIxMjM0NTY3ODkwIn0."
        + "dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U",
    ),
    (
        "private key block",
        "-----BEGIN RSA PRIVATE KEY-----",
    ),
]


@pytest.mark.parametrize(("label", "body"), PATTERN_CASES)
def test_provider_pattern_match_is_reported(label: str, body: str) -> None:
    assert scan({"creds.txt": body.encode()}, []) == [f"creds.txt: {label} {body[:4]}..."]


def test_declared_value_in_a_plain_file_is_reported() -> None:
    files = {"job.json": b'{"agents":[{"kwargs":{"api_key":"' + VALUE.encode() + b'"}}]}'}
    assert scan(files, [VALUE]) == [f"job.json: declared credential {VALUE[:4]}..."]


def test_declared_value_in_a_gzip_member_is_reported() -> None:
    files = {"proxy-requests.jsonl.gz": gzip.compress(VALUE.encode(), mtime=0)}
    assert scan(files, [VALUE]) == [f"proxy-requests.jsonl.gz: declared credential {VALUE[:4]}..."]


def test_problem_text_does_not_contain_the_declared_value() -> None:
    (problem,) = scan({"job.json": VALUE.encode()}, [VALUE])
    assert VALUE not in problem


def test_non_utf8_bytes_are_scanned() -> None:
    files = {"binary.dat": b"\xff\x00" + VALUE.encode() + b"\xfe"}
    assert scan(files, [VALUE]) == [f"binary.dat: declared credential {VALUE[:4]}..."]


def test_clean_files_give_no_problems() -> None:
    files = {"report.md": b"nothing secret here\n", "trials.jsonl": b'{"reward": 1.0}\n'}
    assert scan(files, [VALUE]) == []


def test_digest_is_not_reported_without_a_declared_value() -> None:
    files = {"freeze.json": ('{"config_sha256":"' + VALUE + '"}').encode()}
    assert scan(files, []) == []


def _gzip_with_name(filename: str, payload: bytes) -> bytes:
    buffer = io.BytesIO()
    with gzip.GzipFile(filename=filename, mode="wb", fileobj=buffer, mtime=0) as handle:
        handle.write(payload)
    return buffer.getvalue()


def test_declared_value_in_a_filename_component_is_masked() -> None:
    files = {f"request-bodies/{VALUE}.jsonl": VALUE.encode()}
    assert scan(files, [VALUE]) == [
        f"request-bodies/<credential>: declared credential {VALUE[:4]}..."
    ]


def test_pattern_shaped_key_in_a_filename_component_is_masked() -> None:
    body = "sk-" + "a" * 40
    files = {f"{body}.txt": body.encode()}
    assert scan(files, []) == [f"<credential>: openai-style key {body[:4]}..."]


def test_overlapping_declared_values_mask_the_whole_filename_component() -> None:
    first = "synthetic-overlapping-credential-abc"
    second = "credential-abc-tail"
    files = {f"{first}-tail.txt": first.encode()}
    assert scan(files, [first, second]) == [f"<credential>: declared credential {first[:4]}..."]


def test_nonprintable_filename_characters_are_escaped() -> None:
    files = {"bad\x01name.txt": VALUE.encode()}
    assert scan(files, [VALUE]) == ["bad\\u0001name.txt: declared credential " + VALUE[:4] + "..."]


def test_declared_value_in_a_gzip_filename_header_is_reported() -> None:
    data = _gzip_with_name(VALUE, b"clean payload\n")
    assert scan({"request-bodies.jsonl.gz": data}, [VALUE]) == [
        f"request-bodies.jsonl.gz: declared credential {VALUE[:4]}..."
    ]


def test_provider_pattern_in_a_gzip_payload_is_reported() -> None:
    body = "sk-" + "a" * 40
    files = {"request-bodies.jsonl.gz": gzip.compress(body.encode(), mtime=0)}
    assert scan(files, []) == [f"request-bodies.jsonl.gz: openai-style key {body[:4]}..."]


@pytest.mark.parametrize(
    "data",
    [b"not a gzip member", gzip.compress(b"clean", mtime=0)[:-8]],
)
def test_unreadable_gzip_is_a_generic_problem(data: bytes) -> None:
    assert scan({"broken.jsonl.gz": data}, []) == ["broken.jsonl.gz: unreadable gzip"]


def test_repeated_literal_matches_are_all_reported() -> None:
    files = {"a.txt": (VALUE + "\n" + VALUE).encode()}
    expected = f"a.txt: declared credential {VALUE[:4]}..."
    assert scan(files, [VALUE]) == [expected, expected]


def test_declared_value_order_does_not_change_problems() -> None:
    first = "aaaaaaaa"
    second = "bbbbbbbb"
    files = {"a.txt": (first + " " + second).encode()}
    assert scan(files, [first, second]) == scan(files, [second, first])


def test_key_values_returns_the_set_card_variables() -> None:
    card = {"key_env": ["SECOND_KEY", "FIRST_KEY", "MISSING_KEY"]}
    environment = {"FIRST_KEY": "a" * 8, "SECOND_KEY": "b" * 8}
    assert key_values(card, environment) == ("a" * 8, "b" * 8)


def test_key_values_ignores_a_value_shorter_than_eight_characters() -> None:
    card = {"key_env": ["SHORT_KEY", "EXACT_KEY"]}
    environment = {"SHORT_KEY": "1234567", "EXACT_KEY": "12345678"}
    assert key_values(card, environment) == ("12345678",)


def test_scan_ignores_a_value_shorter_than_eight_characters() -> None:
    assert scan({"job.json": b"the value 1234567 is here"}, ["1234567"]) == []


def test_key_values_strips_surrounding_space() -> None:
    card = {"key_env": ["SPACED_KEY"]}
    environment = {"SPACED_KEY": "  " + "a" * 8 + "  "}
    assert key_values(card, environment) == ("a" * 8,)
