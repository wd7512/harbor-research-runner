import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from harbor_research_runner.docker_host.resources import (
    _parse_pswpout,
    _parse_swapouts,
    host_description,
    host_fingerprint,
    measure,
    spare_memory_mb,
)
from harbor_research_runner.run.loop import HOST_FIELDS


def test_linux_memavailable_is_spare() -> None:
    meminfo = """MemTotal:       16316524 kB
MemFree:          123456 kB
MemAvailable:    8388608 kB
Buffers:           12345 kB
"""

    def read_text(path: Path, **kwargs) -> str:
        assert str(path) == "/proc/meminfo"
        return meminfo

    assert spare_memory_mb(read_text=read_text) == 8192


def test_linux_meminfo_without_memavailable_fails_closed() -> None:
    def read_text(path: Path, **kwargs) -> str:
        return "MemTotal: 1024 kB\nMemFree: 512 kB\n"

    with pytest.raises(ValueError, match="MemAvailable"):
        spare_memory_mb(read_text=read_text)


def test_linux_meminfo_rejects_negative_memavailable() -> None:
    def read_text(path: Path, **kwargs) -> str:
        return "MemAvailable: -1 kB\n"

    with pytest.raises(ValueError, match="negative MemAvailable"):
        spare_memory_mb(read_text=read_text)


def test_linux_meminfo_names_an_unparseable_memavailable_value() -> None:
    def read_text(path: Path, **kwargs) -> str:
        return "MemAvailable: unknown kB\n"

    with pytest.raises(ValueError, match="unparseable MemAvailable"):
        spare_memory_mb(read_text=read_text)


def test_macos_vm_stat_counts_free_reclaimable_pages() -> None:
    vm_stat = """Mach Virtual Memory Statistics: (page size of 4096 bytes)
Pages free:                                  1000.
Pages active:                                5000.
Pages inactive:                              2000.
Pages speculative:                            500.
Pages wired down:                             100.
Pages purgeable:                              250.
Pages occupied by compressor:                 400.
"""

    def read_text(path: Path, **kwargs) -> str:
        raise FileNotFoundError(path)

    def run(argv, **kwargs):
        assert argv == ["vm_stat"]
        return SimpleNamespace(returncode=0, stdout=vm_stat, stderr="")

    assert spare_memory_mb(run=run, read_text=read_text) == ((1000 + 2000 + 500 + 250) * 4096) // (
        1024 * 1024
    )


def test_macos_vm_stat_uses_its_page_size_header() -> None:
    vm_stat = """Mach Virtual Memory Statistics: (page size of 16384 bytes)
Pages free:                                  1000.
Pages inactive:                              2000.
Pages speculative:                            500.
Pages purgeable:                              250.
"""

    def read_text(path: Path, **kwargs) -> str:
        raise FileNotFoundError(path)

    def run(argv, **kwargs):
        assert argv == ["vm_stat"]
        return SimpleNamespace(returncode=0, stdout=vm_stat, stderr="")

    assert spare_memory_mb(run=run, read_text=read_text) == ((1000 + 2000 + 500 + 250) * 16384) // (
        1024 * 1024
    )


def test_macos_vm_stat_without_free_pages_fails_closed() -> None:
    vm_stat = "Mach Virtual Memory Statistics: (page size of 4096 bytes)\n"

    def read_text(path: Path, **kwargs) -> str:
        raise FileNotFoundError(path)

    def run(argv, **kwargs):
        return SimpleNamespace(returncode=0, stdout=vm_stat, stderr="")

    with pytest.raises(ValueError, match="Pages free"):
        spare_memory_mb(run=run, read_text=read_text)


def test_macos_vm_stat_without_page_size_fails_closed() -> None:
    def read_text(path: Path, **kwargs) -> str:
        raise FileNotFoundError(path)

    def run(argv, **kwargs):
        return SimpleNamespace(returncode=0, stdout="Pages free: 1.\n", stderr="")

    with pytest.raises(ValueError, match="page size"):
        spare_memory_mb(run=run, read_text=read_text)


def test_macos_vm_stat_rejects_negative_page_counts() -> None:
    vm_stat = "Mach Virtual Memory Statistics: (page size of 4096 bytes)\nPages free: -1.\n"

    def read_text(path: Path, **kwargs) -> str:
        raise FileNotFoundError(path)

    def run(argv, **kwargs):
        return SimpleNamespace(returncode=0, stdout=vm_stat, stderr="")

    with pytest.raises(ValueError, match="Pages free"):
        spare_memory_mb(run=run, read_text=read_text)


def test_macos_vm_stat_names_an_unparseable_bucket() -> None:
    vm_stat = """Mach Virtual Memory Statistics: (page size of 4096 bytes)
Pages free: many.
"""

    def read_text(path: Path, **kwargs) -> str:
        raise FileNotFoundError(path)

    def run(argv, **kwargs):
        return SimpleNamespace(returncode=0, stdout=vm_stat, stderr="")

    with pytest.raises(ValueError, match="Pages free"):
        spare_memory_mb(run=run, read_text=read_text)


def test_measure_reads_linux_meminfo_and_host_metrics(tmp_path: Path) -> None:
    calls = []

    def read_text(path: Path, **kwargs) -> str:
        if str(path) == "/proc/meminfo":
            return "MemAvailable: 8388608 kB\n"
        if str(path) == "/proc/vmstat":
            return "pswpin 2\npswpout 17\n"
        raise FileNotFoundError(path)

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        if argv[:2] == ["docker", "info"]:
            return SimpleNamespace(returncode=0, stdout="ok", stderr="")
        if argv[:2] == ["docker", "ps"]:
            return SimpleNamespace(
                returncode=0,
                stdout="trial__env-main-1\nother-container\n",
                stderr="",
            )
        raise AssertionError(argv)

    sample, swapouts = measure(run=run, read_text=read_text, disk_path=tmp_path)

    assert sample.mem_available_mb == 8192
    assert sample.swapped_out_pages == 0
    assert swapouts == 17
    assert sample.disk_free_gb > 0
    assert sample.trial_containers == 1
    assert sample.docker_ok
    assert all(kwargs["timeout"] == 5 for _, kwargs in calls if _[:2] == ["docker", "info"])


def test_measure_uses_macos_vm_stat_swapouts_when_proc_is_absent(tmp_path: Path) -> None:
    def read_text(path: Path, **kwargs) -> str:
        raise FileNotFoundError(path)

    def run(argv, **kwargs):
        if argv == ["vm_stat"]:
            return SimpleNamespace(
                returncode=0,
                stdout=(
                    "Mach Virtual Memory Statistics: (page size of 4096 bytes)\n"
                    "Pages free: 1000.\nPages inactive: 2000.\n"
                    "Pages speculative: 500.\nPages purgeable: 250.\n"
                    "Swapouts: 128.\n"
                ),
                stderr="",
            )
        if argv[:2] == ["docker", "info"]:
            return SimpleNamespace(returncode=0, stdout="ok", stderr="")
        if argv[:2] == ["docker", "ps"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        raise AssertionError(argv)

    sample, swapouts = measure(run=run, read_text=read_text, disk_path=tmp_path)

    assert sample.mem_available_mb == ((1000 + 2000 + 500 + 250) * 4096) // (1024 * 1024)
    assert sample.swapped_out_pages == 0
    assert swapouts == 128
    assert sample.docker_ok


def test_swapout_parsers_read_real_fixtures() -> None:
    fixtures = Path(__file__).parents[2] / "fixtures"
    vm_stat = (fixtures / "vm_stat-macos.txt").read_text(encoding="utf-8")
    vmstat = (fixtures / "proc-vmstat-linux.txt").read_text(encoding="utf-8")

    assert _parse_swapouts(vm_stat) == int(
        re.search(r"^Swapouts:\s*(\d+)", vm_stat, re.MULTILINE)[1]
    )
    assert _parse_pswpout(vmstat) == int(re.search(r"^pswpout\s+(\d+)", vmstat, re.MULTILINE)[1])


def test_measure_first_sample_and_interval_swapouts(tmp_path: Path) -> None:
    counter = 12

    def read_text(path: Path, **kwargs) -> str:
        if str(path) == "/proc/meminfo":
            return "MemAvailable: 8388608 kB\n"
        if str(path) == "/proc/vmstat":
            return f"pswpout {counter}\n"
        raise FileNotFoundError(path)

    def run(argv, **kwargs):
        if argv[:2] == ["docker", "info"]:
            return SimpleNamespace(returncode=0, stdout="ok", stderr="")
        if argv[:2] == ["docker", "ps"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        raise AssertionError(argv)

    first, first_counter = measure(None, run, read_text, tmp_path)
    counter = 15
    second, second_counter = measure(first_counter, run, read_text, tmp_path)
    counter = 2
    reset, reset_counter = measure(second_counter, run, read_text, tmp_path)

    assert first.swapped_out_pages == 0
    assert second.swapped_out_pages == 3
    assert reset.swapped_out_pages == 0
    assert reset_counter == 2


def test_docker_down_marks_measurement_unhealthy(tmp_path: Path) -> None:
    def read_text(path: Path, **kwargs) -> str:
        if str(path) == "/proc/meminfo":
            return "MemAvailable: 1024 kB\nSwapTotal: 0 kB\nSwapFree: 0 kB\n"
        if str(path) == "/proc/vmstat":
            return "pswpout 0\n"
        raise FileNotFoundError(path)

    def run(argv, **kwargs):
        raise FileNotFoundError("docker")

    sample, _ = measure(run=run, read_text=read_text, disk_path=tmp_path)

    assert not sample.docker_ok


def test_host_description_records_hostname_and_profile_fields() -> None:
    fixture = Path(__file__).parents[2] / "fixtures" / "docker_info" / "docker-desktop-macos.json"
    payload = fixture.read_text(encoding="utf-8")
    networks = (
        Path(__file__).parents[2]
        / "fixtures"
        / "docker_network"
        / "docker-network-ls-desktop.jsonl"
    ).read_text(encoding="utf-8")

    def run(argv, **kwargs):
        if argv == ["docker", "info", "--format", "{{json .}}"]:
            return SimpleNamespace(returncode=0, stdout=payload, stderr="")
        assert argv == ["docker", "network", "ls", "--format", "{{json .}}"]
        return SimpleNamespace(returncode=0, stdout=networks, stderr="")

    description = host_description(run=run)

    assert set(description) == HOST_FIELDS
    assert description["hostname"]
    assert description["host_profile"]["memory_cap"] == "kernel"
    assert description["host_profile"]["rootless"] is False
    assert description["host_profile"]["container_network"] == "bridge"


def test_host_fingerprint_fails_when_docker_is_unavailable() -> None:
    def run(argv, **kwargs):
        raise FileNotFoundError(argv[0])

    with pytest.raises(FileNotFoundError):
        host_fingerprint(run=run)
