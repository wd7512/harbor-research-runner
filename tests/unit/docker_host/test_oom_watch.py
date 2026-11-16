import os

from harbor_research_runner.docker_host.oom_watch import append_event_line
from harbor_research_runner.records.jsonl import read_rows


def test_docker_event_line_is_appended_as_jsonl(tmp_path):
    path = tmp_path / "docker-oom-events.jsonl"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        append_event_line(fd, '{"Type":"container","Action":"oom"}\n')
    finally:
        os.close(fd)

    assert read_rows(path).rows == [{"Type": "container", "Action": "oom"}]


def test_invalid_docker_event_line_is_rejected(tmp_path):
    path = tmp_path / "docker-oom-events.jsonl"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        try:
            append_event_line(fd, "[]\n")
        except ValueError as exc:
            assert "JSON object" in str(exc)
        else:
            raise AssertionError("non-object Docker event should be rejected")
    finally:
        os.close(fd)
