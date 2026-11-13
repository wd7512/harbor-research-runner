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


def test_proc_memory_sums_pss_over_process_tree_and_skips_disappeared_process():
    def run(_command):
        return SimpleNamespace(
            stdout=(FIXTURES / "docker-inspect-state.json").read_text(encoding="utf-8")
        )

    def read_text(path):
        path = str(path)
        if path.endswith("/618/smaps_rollup"):
            return (FIXTURES / "proc-smaps-rollup.txt").read_text(encoding="utf-8")
        if path.endswith("/618/task/618/children"):
            return "8 "
        if path.endswith("/8/smaps_rollup"):
            return "Pss: 100 kB\n"
        if path.endswith("/8/task/8/children"):
            return "9 "
        raise FileNotFoundError(path)

    def list_dir(path):
        path = str(path)
        if path.endswith("/618/task"):
            return [Path(path) / "618"]
        if path.endswith("/8/task"):
            return [Path(path) / "8"]
        raise FileNotFoundError(path)

    used_mb, reader = proc_container_memory("be3b8bd3cf84", run, read_text, list_dir)

    assert used_mb == pytest.approx((916 + 100) / 1024)
    assert reader == "proc_pss"


def test_proc_memory_falls_back_to_rss_for_a_process_refused_by_smaps():
    def run(_command):
        return SimpleNamespace(
            stdout=(FIXTURES / "docker-inspect-state.json").read_text(encoding="utf-8")
        )

    def read_text(path):
        path = str(path)
        if path.endswith("/618/smaps_rollup"):
            return (FIXTURES / "proc-smaps-rollup.txt").read_text(encoding="utf-8")
        if path.endswith("/618/task/618/children"):
            return "8 "
        if path.endswith("/8/smaps_rollup"):
            raise PermissionError(path)
        if path.endswith("/8/status"):
            return "VmRSS: 2048 kB\n"
        if path.endswith("/8/task/8/children"):
            return ""
        raise FileNotFoundError(path)

    def list_dir(path):
        path = str(path)
        if path.endswith("/618/task"):
            return [Path(path) / "618"]
        if path.endswith("/8/task"):
            return [Path(path) / "8"]
        raise FileNotFoundError(path)

    used_mb, reader = proc_container_memory("be3b8bd3cf84", run, read_text, list_dir)

    assert used_mb == pytest.approx((916 + 2048) / 1024)
    assert reader == "proc_rss"


def test_proc_memory_parses_pss_without_matching_pss_dirty():
    def run(_command):
        return SimpleNamespace(
            stdout=(FIXTURES / "docker-inspect-state.json").read_text(encoding="utf-8")
        )

    def read_text(path):
        path = str(path)
        if path.endswith("/618/smaps_rollup"):
            return "Pss_Dirty: 999 kB\nPss: 412 kB\n"
        if path.endswith("/618/task/618/children"):
            return ""
        raise FileNotFoundError(path)

    def list_dir(path):
        return [Path(path) / "618"]

    used_mb, reader = proc_container_memory("be3b8bd3cf84", run, read_text, list_dir)

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


def test_proc_memory_skips_a_child_that_exits_after_its_tasks_were_listed():
    def read_text(path):
        path = str(path)
        if path.endswith("/618/smaps_rollup"):
            return (FIXTURES / "proc-smaps-rollup.txt").read_text(encoding="utf-8")
        if path.endswith("/618/task/618/children"):
            return "8 "
        raise FileNotFoundError(path)

    def list_dir(path):
        return [Path(path) / Path(path).parent.name]

    used_mb, reader = proc_rss_mb(618, read_text=read_text, list_dir=list_dir)

    assert used_mb == pytest.approx(916 / 1024)
    assert reader == "proc_pss"
