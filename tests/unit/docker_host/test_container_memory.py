import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from harbor_research_runner.docker_host.container_memory import (
    compose_project_label,
    docker_stats_memory,
    parse_mem_usage,
    proc_container_memory,
    proc_rss_mb,
    reader_for_memory_cap,
)

FIXTURES = Path(__file__).parents[2] / "fixtures" / "memory_watch"


@pytest.mark.parametrize(
    ("value", "expected"),
    [("1B", 1 / 1024**2), ("1KiB", 1 / 1024), ("1MiB", 1.0), ("1GiB", 1024.0)],
)
def test_mem_usage_parses_supported_units(value, expected):
    assert parse_mem_usage(f"{value} / 256MiB") == pytest.approx(expected)


def test_proc_memory_sums_pss_for_every_process_in_container_pid_namespace():
    def run(_command):
        return SimpleNamespace(stdout=json.dumps({"Pid": 100}))

    def read_text(path):
        path = str(path)
        if path.endswith("/100/smaps_rollup"):
            return "Pss: 2048 kB\n"
        if path.endswith("/200/smaps_rollup"):
            return "Pss: 307200 kB\n"
        if path.endswith("/300/smaps_rollup"):
            return "Pss: 999999 kB\n"
        raise FileNotFoundError(path)

    def list_dir(path):
        assert path == Path("/proc")
        return [Path("/proc") / str(pid) for pid in (100, 200, 300)]

    def readlink(path):
        links = {
            "/proc/100/ns/pid": "pid:[4026532001]",
            "/proc/200/ns/pid": "pid:[4026532001]",
            "/proc/300/ns/pid": "pid:[4026531836]",
        }
        return links[str(path)]

    used_mb, reader = proc_container_memory("be3b8bd3cf84", run, read_text, list_dir, readlink)

    assert used_mb == pytest.approx((2048 + 307200) / 1024)
    assert reader == "proc_pss"


def test_proc_memory_falls_back_to_rss_for_a_process_refused_by_smaps():
    def run(_command):
        return SimpleNamespace(stdout=json.dumps({"Pid": 100}))

    def read_text(path):
        path = str(path)
        if path.endswith("/100/smaps_rollup"):
            return "Pss: 916 kB\n"
        if path.endswith("/200/smaps_rollup"):
            raise PermissionError(path)
        if path.endswith("/200/status"):
            return "VmRSS: 2048 kB\n"
        raise FileNotFoundError(path)

    def list_dir(_path):
        return [Path("/proc/100"), Path("/proc/200")]

    used_mb, reader = proc_container_memory(
        "be3b8bd3cf84", run, read_text, list_dir, lambda _path: "pid:[4026532001]"
    )

    assert used_mb == pytest.approx((916 + 2048) / 1024)
    assert reader == "proc_rss"


def test_proc_memory_parses_pss_without_matching_pss_dirty():
    def run(_command):
        return SimpleNamespace(stdout=json.dumps({"Pid": 618}))

    def read_text(path):
        path = str(path)
        if path.endswith("/618/smaps_rollup"):
            return "Pss_Dirty: 999 kB\nPss: 412 kB\n"
        raise FileNotFoundError(path)

    def list_dir(_path):
        return [Path("/proc/618")]

    used_mb, reader = proc_container_memory(
        "be3b8bd3cf84", run, read_text, list_dir, lambda _path: "pid:[4026532001]"
    )

    assert used_mb == pytest.approx(412 / 1024)
    assert reader == "proc_pss"


def test_reader_is_selected_from_memory_cap():
    assert reader_for_memory_cap("kernel") == "docker_stats"
    assert reader_for_memory_cap("none") == "proc_pss"


def test_docker_stats_reader_maps_fixture_to_container_id():
    stats = (FIXTURES / "docker-stats-macos.jsonl").read_text(encoding="utf-8")
    calls = []

    def run(command):
        calls.append(command)
        if "stats" in command:
            return SimpleNamespace(stdout=stats)
        return SimpleNamespace(
            stdout=json.dumps(
                {
                    "ID": "be3b8bd3cf84",
                    "Names": "hrr-fixture-mem",
                    "Labels": "com.docker.compose.project=trial-a__abc__env",
                }
            )
        )

    containers = [
        {
            "ID": "be3b8bd3cf84",
            "Names": "hrr-fixture-mem",
            "Labels": "com.docker.compose.project=trial-a__abc__env",
        }
    ]
    assert docker_stats_memory(run, containers) == {"be3b8bd3cf84": pytest.approx(3.758)}
    assert len(calls) == 1


def test_compose_project_label_reads_docker_ps_labels():
    row = json.loads((FIXTURES / "docker-inspect-labels.json").read_text(encoding="utf-8"))

    assert compose_project_label({"Labels": row}) == "trial-a__abc__env"


def test_compose_project_label_reads_formatted_docker_ps_label_string():
    row = {"Labels": "other=value,com.docker.compose.project=trial-a__abc__env"}

    assert compose_project_label(row) == "trial-a__abc__env"


def test_proc_memory_skips_a_process_that_vanishes_mid_scan():
    def read_text(path):
        path = str(path)
        if path.endswith("/618/smaps_rollup"):
            return (FIXTURES / "proc-smaps-rollup.txt").read_text(encoding="utf-8")
        raise FileNotFoundError(path)

    def list_dir(_path):
        return [Path("/proc/618"), Path("/proc/619")]

    def readlink(path):
        if str(path) == "/proc/619/ns/pid":
            raise FileNotFoundError(path)
        return "pid:[4026532001]"

    used_mb, reader = proc_rss_mb(618, read_text=read_text, list_dir=list_dir, readlink=readlink)

    assert used_mb == pytest.approx(916 / 1024)
    assert reader == "proc_pss"


def test_proc_memory_raises_when_init_namespace_cannot_be_read():
    def readlink(_path):
        raise PermissionError("unreadable namespace")

    with pytest.raises(PermissionError, match="unreadable namespace"):
        proc_rss_mb(100, readlink=readlink)


def test_docker_stats_reader_matches_unfiltered_stats_rows_keyed_by_full_id():
    stats = (FIXTURES / "docker-stats-all-containers.jsonl").read_text(encoding="utf-8")

    def run(command):
        return SimpleNamespace(stdout=stats)

    containers = [{"ID": "2abc7dd4e196", "Names": "hrr-fixture-mem"}]

    assert docker_stats_memory(run, containers) == {"2abc7dd4e196": pytest.approx(5.277)}
