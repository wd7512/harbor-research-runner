from pathlib import Path
from types import SimpleNamespace

from harbor_research_runner.docker_host.resources import host_fingerprint, measure


def test_measure_reads_linux_meminfo_and_host_metrics(tmp_path: Path) -> None:
    calls = []

    def read_text(path: Path, **kwargs) -> str:
        if str(path) == "/proc/meminfo":
            return "MemAvailable: 8388608 kB\nSwapTotal: 2097152 kB\nSwapFree: 1048576 kB\n"
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

    sample = measure(run=run, read_text=read_text, disk_path=tmp_path)

    assert sample.mem_available_mb == 8192
    assert sample.swap_used_mb == 1024
    assert sample.disk_free_gb > 0
    assert sample.trial_containers == 1
    assert sample.docker_ok
    assert all(kwargs["timeout"] == 5 for _, kwargs in calls if _[:2] == ["docker", "info"])


def test_measure_uses_macos_vm_stat_and_sysctl_when_proc_is_absent(tmp_path: Path) -> None:
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
                ),
                stderr="",
            )
        if argv == ["sysctl", "vm.swapusage"]:
            return SimpleNamespace(
                returncode=0, stdout="total = 1024.00M used = 128.00M free = 896.00M\n", stderr=""
            )
        if argv[:2] == ["docker", "info"]:
            return SimpleNamespace(returncode=0, stdout="ok", stderr="")
        if argv[:2] == ["docker", "ps"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        raise AssertionError(argv)

    sample = measure(run=run, read_text=read_text, disk_path=tmp_path)

    assert sample.mem_available_mb == ((1000 + 2000 + 500 + 250) * 4096) // (1024 * 1024)
    assert sample.swap_used_mb == 128
    assert sample.docker_ok


def test_docker_down_marks_measurement_unhealthy(tmp_path: Path) -> None:
    def read_text(path: Path, **kwargs) -> str:
        if str(path) == "/proc/meminfo":
            return "MemAvailable: 1024 kB\nSwapTotal: 0 kB\nSwapFree: 0 kB\n"
        raise FileNotFoundError(path)

    def run(argv, **kwargs):
        raise FileNotFoundError("docker")

    sample = measure(run=run, read_text=read_text, disk_path=tmp_path)

    assert not sample.docker_ok


def test_host_fingerprint_and_description_keep_host_identity_fields() -> None:
    def run(argv, **kwargs):
        if argv[:2] == ["docker", "version"]:
            return SimpleNamespace(returncode=0, stdout="29.7.2\n", stderr="")
        raise AssertionError(argv)

    fingerprint = host_fingerprint(run=run)

    assert len(fingerprint) == 12
    assert host_fingerprint(run=run) == fingerprint


def test_host_fingerprint_survives_unavailable_docker() -> None:
    def run(argv, **kwargs):
        raise FileNotFoundError(argv[0])

    assert len(host_fingerprint(run=run)) == 12
