from __future__ import annotations

import subprocess
import threading
from pathlib import Path

import pytest

from harbor_research_runner.harness.extract import (
    ExtractedHarness,
    ExtractError,
    cleanup,
    extract,
)
from harbor_research_runner.harness.manifest import ImageEntry

COMMIT = "a" * 40
DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
REF = "hrr/opencode:aaaaaaaaaaaa-amd64"
BINARY_BYTES = b"\x7fELF" + b"\x00" * 100


def image_entry(*, ref: str = REF, digest: str = DIGEST_A) -> ImageEntry:
    return ImageEntry(
        harness="opencode",
        commit=COMMIT,
        version="1.2.3",
        arch="amd64",
        ref=ref,
        digest=digest,
        build_method="source",
        built_at="2026-11-11",
        withdrawn=False,
        withdrawn_reason="",
    )


def _ok(stdout: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr="")


def _fail(stderr: str = "boom", returncode: int = 1) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout="", stderr=stderr)


def _lay_out_usr_local(dst: str, layout: str, *, empty: bool = False) -> None:
    root = Path(dst.rstrip("/."))
    bin_dir = root / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    if layout == "missing":
        return
    if layout == "hard":
        path = bin_dir / "opencode"
        if empty:
            path.touch()
        else:
            path.write_bytes(BINARY_BYTES)
        return
    lib_dir = root / "lib" / "node_modules" / "opencode-ai" / "bin"
    lib_dir.mkdir(parents=True, exist_ok=True)
    target = lib_dir / "opencode"
    if empty:
        target.touch()
    else:
        target.write_bytes(BINARY_BYTES)
    (bin_dir / "opencode").symlink_to("../lib/node_modules/opencode-ai/bin/opencode")


class FakeDocker:
    def __init__(
        self,
        *,
        layout: str = "symlink",
        empty: bool = False,
        create_fails: bool = False,
        cp_fails: bool = False,
        cp_started: threading.Event | None = None,
        cp_release: threading.Event | None = None,
    ) -> None:
        self.layout = layout
        self.empty = empty
        self.create_fails = create_fails
        self.cp_fails = cp_fails
        self.cp_started = cp_started
        self.cp_release = cp_release
        self.calls: list[list[str]] = []
        self.timeouts: list[int | None] = []
        self._active_cp = 0
        self._max_active_cp = 0
        self._counter_lock = threading.Lock()

    @property
    def max_concurrent_cp(self) -> int:
        with self._counter_lock:
            return self._max_active_cp

    def count(self, prefix: list[str]) -> int:
        return sum(1 for call in self.calls if call[: len(prefix)] == prefix)

    def __call__(self, cmd, **kwargs):
        with self._counter_lock:
            self.calls.append(list(cmd))
            self.timeouts.append(kwargs.get("timeout"))
        if cmd[:2] == ["docker", "create"]:
            if self.create_fails:
                return _fail("no such image", returncode=125)
            return _ok()
        if cmd[:2] == ["docker", "cp"]:
            _, source = cmd[2].split(":", 1)
            if source == "/usr/local/.":
                with self._counter_lock:
                    self._active_cp += 1
                    self._max_active_cp = max(self._max_active_cp, self._active_cp)
                try:
                    if self.cp_started is not None:
                        self.cp_started.set()
                    if self.cp_release is not None:
                        self.cp_release.wait(timeout=5)
                    if self.cp_fails:
                        return _fail("cp failed")
                    _lay_out_usr_local(cmd[3], self.layout, empty=self.empty)
                    return _ok()
                finally:
                    with self._counter_lock:
                        self._active_cp -= 1
            if source == "/opt/opencode-commit.txt":
                if self.layout == "symlink":
                    Path(cmd[3]).write_text("deadbeef\n", encoding="utf-8")
                    return _ok()
                return _fail("not present")
            return _fail(f"unexpected cp source={source}")
        if cmd[:2] == ["docker", "rm"]:
            return _ok()
        return _fail(f"unexpected command: {cmd}")


def run_extract(
    workdir: Path,
    *,
    entry: ImageEntry | None = None,
    docker: FakeDocker | None = None,
) -> ExtractedHarness:
    return extract(entry or image_entry(), workdir=workdir, run=docker or FakeDocker())


def test_extract_refuses_image_without_binary(tmp_path: Path) -> None:
    workdir = tmp_path / "work"
    with pytest.raises(ExtractError, match="no /usr/local/bin/opencode"):
        run_extract(workdir, docker=FakeDocker(layout="missing"))


def test_extract_uses_given_workdir(tmp_path: Path) -> None:
    workdir = tmp_path / "chosen"
    extracted = run_extract(workdir)
    assert extracted.local_root == workdir / ("a" * 64) / "root"
    assert extracted.local_root.is_relative_to(workdir)
    assert extracted.binary_path.is_symlink()


def test_extract_uses_manifest_digest_without_docker_inspect(tmp_path: Path) -> None:
    docker = FakeDocker()
    extracted = run_extract(tmp_path / "work", docker=docker)
    assert extracted.local_root.parent.name == "a" * 64
    assert docker.count(["docker", "image", "inspect"]) == 0
    assert docker.count(["docker", "create"]) == 1


def test_extract_uses_fixed_600_second_docker_timeout(tmp_path: Path) -> None:
    docker = FakeDocker()
    run_extract(tmp_path / "work", docker=docker)

    boundary = {("docker", "create"), ("docker", "cp"), ("docker", "rm")}
    observed = [
        (tuple(cmd[:2]), timeout)
        for cmd, timeout in zip(docker.calls, docker.timeouts, strict=True)
        if tuple(cmd[:2]) in boundary
    ]
    assert {prefix for prefix, _ in observed} == boundary
    assert all(timeout == 600 for _, timeout in observed)


def test_extract_reads_symlink_layout(tmp_path: Path) -> None:
    extracted = run_extract(tmp_path / "work", docker=FakeDocker(layout="symlink"))
    assert extracted.binary_path.is_symlink()
    assert extracted.binary_path.resolve() == (
        extracted.local_root / "lib" / "node_modules" / "opencode-ai" / "bin" / "opencode"
    )
    assert extracted.commit_sha == "deadbeef"
    assert extracted.opencode_version is None


def test_extract_reads_hard_install_layout(tmp_path: Path) -> None:
    extracted = run_extract(tmp_path / "work", docker=FakeDocker(layout="hard"))
    assert extracted.binary_path.is_file()
    assert not extracted.binary_path.is_symlink()
    assert extracted.commit_sha is None


@pytest.mark.parametrize("layout", ["hard", "symlink"], ids=["empty_hard", "empty_symlink"])
def test_extract_rejects_empty_binary(tmp_path: Path, layout: str) -> None:
    with pytest.raises(ExtractError, match="is empty"):
        run_extract(tmp_path / "work", docker=FakeDocker(layout=layout, empty=True))


def test_extract_fails_on_docker_create_failure(tmp_path: Path) -> None:
    with pytest.raises(ExtractError, match=f"docker create {REF!r} failed"):
        run_extract(tmp_path / "work", docker=FakeDocker(create_fails=True))


def test_extract_always_runs_docker_rm_even_on_failure(tmp_path: Path) -> None:
    docker = FakeDocker(cp_fails=True)
    with pytest.raises(ExtractError, match="docker cp"):
        run_extract(tmp_path / "work", docker=docker)
    assert docker.count(["docker", "rm"]) == 1


def test_second_extract_of_same_image_makes_no_docker_calls(tmp_path: Path) -> None:
    workdir = tmp_path / "work"
    docker = FakeDocker()
    first = run_extract(workdir, docker=docker)
    assert first.from_cache is False
    assert first.binary_path.is_symlink()
    assert docker.count(["docker", "create"]) == 1
    assert docker.count(["docker", "cp"]) == 2

    second = run_extract(workdir, docker=docker)
    assert second.from_cache is True
    assert second.local_root == first.local_root
    assert docker.count(["docker", "create"]) == 1
    assert docker.count(["docker", "cp"]) == 2


def test_cache_entry_is_keyed_on_digest_not_ref(tmp_path: Path) -> None:
    workdir = tmp_path / "work"
    docker = FakeDocker()
    first = run_extract(workdir, entry=image_entry(ref="hrr/opencode:one"), docker=docker)
    second = run_extract(workdir, entry=image_entry(ref="hrr/opencode:alias"), docker=docker)
    assert first.local_root == second.local_root
    assert second.from_cache is True
    assert docker.count(["docker", "create"]) == 1


def test_distinct_digests_get_distinct_entries(tmp_path: Path) -> None:
    workdir = tmp_path / "work"
    docker = FakeDocker()
    first = run_extract(workdir, entry=image_entry(digest=DIGEST_A), docker=docker)
    second = run_extract(workdir, entry=image_entry(digest=DIGEST_B), docker=docker)
    assert first.local_root != second.local_root
    assert second.from_cache is False
    assert docker.count(["docker", "create"]) == 2


def test_concurrent_extracts_of_one_image_issue_at_most_one_copy(tmp_path: Path) -> None:
    workdir = tmp_path / "work"
    cp_started = threading.Event()
    cp_release = threading.Event()
    docker = FakeDocker(cp_started=cp_started, cp_release=cp_release)
    results: list[ExtractedHarness] = []
    errors: list[BaseException] = []
    barrier = threading.Barrier(2)

    def worker() -> None:
        barrier.wait()
        try:
            results.append(run_extract(workdir, docker=docker))
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    assert cp_started.wait(timeout=5)
    cp_release.set()
    for thread in threads:
        thread.join(timeout=30)

    assert not errors, errors
    assert len(results) == 2
    assert docker.max_concurrent_cp == 1
    assert docker.count(["docker", "create"]) == 1
    assert {result.local_root for result in results} == {results[0].local_root}


def test_failed_extraction_leaves_no_entry_and_releases_lock(tmp_path: Path) -> None:
    workdir = tmp_path / "work"
    with pytest.raises(ExtractError, match="docker cp"):
        run_extract(workdir, docker=FakeDocker(cp_fails=True))

    entries = [path for path in workdir.iterdir() if path.is_dir()]
    assert entries == []
    assert not list(workdir.rglob(".complete"))

    working = FakeDocker()
    assert run_extract(workdir, docker=working).from_cache is False
    assert working.count(["docker", "create"]) == 1


def test_cleanup_never_removes_a_cache_entry(tmp_path: Path) -> None:
    workdir = tmp_path / "work"
    extracted = run_extract(workdir)
    entry = extracted.local_root.parent

    cleanup(entry)
    assert extracted.local_root.is_dir()

    cleanup(workdir)
    assert extracted.local_root.is_dir()

    scratch = tmp_path / "unrelated-scratch"
    scratch.mkdir(parents=True)
    cleanup(scratch)
    assert not scratch.exists()


@pytest.mark.parametrize("descendant", ["local_root", "bin"], ids=["local_root", "bin"])
def test_cleanup_never_removes_a_completed_entry_descendant(
    tmp_path: Path, descendant: str
) -> None:
    workdir = tmp_path / "work"
    extracted = run_extract(workdir)
    target = extracted.local_root
    if descendant == "bin":
        target = target / "bin"

    cleanup(target)

    assert target.is_dir()
    assert extracted.binary_path.read_bytes() == BINARY_BYTES
