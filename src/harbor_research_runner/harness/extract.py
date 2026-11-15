"""Extract a pinned harness image's files on the host, cached by image digest."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from harbor_research_runner.docker_host import images
from harbor_research_runner.harness.manifest import ImageEntry

logger = logging.getLogger(__name__)

DEFAULT_DOCKER_TIMEOUT_S = 600
_COMPLETE_MARKER = ".complete"

DockerRunner = Callable[..., subprocess.CompletedProcess[str]]


class ExtractError(RuntimeError):
    """Raised when the harness image cannot be extracted on the host."""


@dataclass(frozen=True)
class ExtractedHarness:
    """A harness extracted from a pinned image.

    ``local_root`` is the path to a directory tree that should be uploaded as
    ``/usr/local`` in the task container. It contains at minimum
    ``bin/opencode`` and, for npm-mode images, the full
    ``lib/node_modules/opencode-ai/`` tree.

    ``local_root`` lives inside the digest cache and is shared by every later
    ``extract`` for the same image digest; callers must treat it as read-only.
    """

    ref: str
    local_root: Path
    binary_path: Path
    commit_sha: str | None
    opencode_version: str | None
    from_cache: bool = False


try:
    import fcntl
except ImportError:
    fcntl = None


def _entry_for(root: Path, digest: str) -> Path:
    return root / digest.replace("sha256:", "").replace(":", "_")


def _is_complete(entry: Path) -> bool:
    return (entry / _COMPLETE_MARKER).is_file()


@contextmanager
def _entry_lock(lock_path: Path) -> Iterator[None]:
    if fcntl is None:
        yield
        return
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def _harness(entry: ImageEntry, entry_dir: Path, *, from_cache: bool) -> ExtractedHarness:
    commit_file = entry_dir / "commit.txt"
    commit_sha = commit_file.read_text(encoding="utf-8").strip() if commit_file.is_file() else None
    return ExtractedHarness(
        ref=entry.ref,
        local_root=entry_dir / "root",
        binary_path=entry_dir / "root" / "bin" / "opencode",
        commit_sha=commit_sha,
        opencode_version=None,
        from_cache=from_cache,
    )


def _validate_binary(binary: Path, ref: str, local_root: Path) -> None:
    if not binary.exists():
        found = list(local_root.rglob("opencode*"))[:5]
        msg = f"no /usr/local/bin/opencode in extracted {ref!r} (found: {found})"
        raise ExtractError(msg)
    try:
        leaf_stat = binary.resolve(strict=True).stat()
    except (OSError, RuntimeError) as exc:
        msg = f"could not stat extracted binary {binary}: {exc}"
        raise ExtractError(msg) from exc
    if leaf_stat.st_size == 0:
        msg = f"extracted binary {binary} is empty"
        raise ExtractError(msg)


def _mark_executable(binary: Path) -> None:
    try:
        target = binary.resolve(strict=True)
        target.chmod(0o755)
    except (OSError, RuntimeError) as exc:
        logger.warning("could not chmod extracted binary target %s: %s", binary, exc)


def _extract_into(entry: ImageEntry, entry_dir: Path, run: DockerRunner) -> None:
    local_root = entry_dir / "root"
    local_root.mkdir(parents=True, exist_ok=True)
    cid = f"harbor_research_runner-{uuid.uuid4().hex[:12]}"

    create = images.create(entry.ref, cid, run, DEFAULT_DOCKER_TIMEOUT_S)
    if create.returncode != 0:
        msg = f"docker create {entry.ref!r} failed (exit {create.returncode}): {create.stderr.strip()}"
        raise ExtractError(msg)

    try:
        cp = images.copy(
            f"{cid}:/usr/local/.", str(local_root) + "/.", run, DEFAULT_DOCKER_TIMEOUT_S
        )
        if cp.returncode != 0:
            msg = f"docker cp {entry.ref!r}:/usr/local failed (exit {cp.returncode}): {cp.stderr.strip()}"
            raise ExtractError(msg)
        binary = local_root / "bin" / "opencode"
        _validate_binary(binary, entry.ref, local_root)
        _mark_executable(binary)
        images.copy(
            f"{cid}:/opt/opencode-commit.txt",
            str(entry_dir / "commit.txt"),
            run,
            DEFAULT_DOCKER_TIMEOUT_S,
        )
    finally:
        images.remove(cid, run, DEFAULT_DOCKER_TIMEOUT_S)


def _publish(staging: Path, entry: Path) -> None:
    try:
        os.rename(staging, entry)
    except OSError:
        if not _is_complete(entry):
            raise
        shutil.rmtree(staging, ignore_errors=True)


def extract(
    entry: ImageEntry,
    *,
    workdir: Path,
    run: DockerRunner = subprocess.run,
) -> ExtractedHarness:
    """Extract the harness files from the pinned image on the host.

    The whole ``/usr/local`` is copied, which holds both the binary and the
    npm ``node_modules/opencode-ai/`` tree. The result is a ``local_root``
    directory you can upload to ``/usr/local`` in the task container.

    The extracted tree is cached under ``workdir``, keyed by the manifest
    entry's image digest, so repeated calls for the same image make no docker
    calls at all.

    Failures (a copy or create that fails, no binary at the expected path)
    raise ``ExtractError`` with a clear message and leave no partial cache
    entry behind.
    """
    root = Path(workdir)
    cache_entry = _entry_for(root, entry.digest)
    if _is_complete(cache_entry):
        logger.info(
            "harbor_research_runner harness cache hit for %s (%s)", entry.ref, cache_entry.name
        )
        return _harness(entry, cache_entry, from_cache=True)

    root.mkdir(parents=True, exist_ok=True)
    with _entry_lock(root / f"{cache_entry.name}.lock"):
        if _is_complete(cache_entry):
            logger.info(
                "harbor_research_runner harness cache hit for %s after lock (%s)",
                entry.ref,
                cache_entry.name,
            )
            return _harness(entry, cache_entry, from_cache=True)
        staging = Path(tempfile.mkdtemp(prefix=".staging-", dir=root))
        try:
            _extract_into(entry, staging, run)
            (staging / _COMPLETE_MARKER).write_text("", encoding="utf-8")
            _publish(staging, cache_entry)
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise
    logger.info(
        "harbor_research_runner harness cache populated for %s (%s)", entry.ref, cache_entry.name
    )
    return _harness(entry, cache_entry, from_cache=False)


def _holds_extraction(path: Path) -> bool:
    if (path / _COMPLETE_MARKER).is_file():
        return True
    return any((child / _COMPLETE_MARKER).is_file() for child in path.iterdir() if child.is_dir())


def _is_inside_extraction(path: Path) -> bool:
    return any((parent / _COMPLETE_MARKER).is_file() for parent in path.parents)


def cleanup(workdir: Path) -> None:
    """Best-effort cleanup of a scratch workdir owned by the caller.

    Refuses to delete a completed extraction entry, any directory holding one,
    or any descendant of one: ``extract`` hands back a ``local_root`` that
    lives in the cache and is shared by every later call for the same image
    digest.
    """
    target = Path(workdir).resolve()
    if target.is_dir() and (_holds_extraction(target) or _is_inside_extraction(target)):
        logger.warning("refusing to remove harness cache path %s", target)
        return
    shutil.rmtree(workdir, ignore_errors=True)
