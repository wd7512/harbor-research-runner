from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import tomllib
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import date
from pathlib import Path


class ManifestError(ValueError):
    pass


@dataclass(frozen=True)
class ImageEntry:
    harness: str
    commit: str
    version: str
    arch: str
    ref: str
    digest: str
    build_method: str
    built_at: str
    withdrawn: bool
    withdrawn_reason: str


_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_ARCHES = {"amd64", "arm64"}
_COMMIT_KEYS = {"version", "withdrawn", "withdrawn_reason", "images"}
_IMAGE_KEYS = {"ref", "digest", "build_method", "built_at"}
_BUILD_METHODS = {"source", "release-package"}


def _expect_string(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise ManifestError(f"{name} must be a string")
    return value


def _parse_date(value: object) -> str:
    built_at = _expect_string(value, "built_at")
    try:
        parsed = date.fromisoformat(built_at)
    except ValueError as exc:
        raise ManifestError("built_at must be a YYYY-MM-DD date") from exc
    if parsed.isoformat() != built_at:
        raise ManifestError("built_at must be a YYYY-MM-DD date")
    return built_at


def load_manifest(path: Path) -> dict[tuple[str, str, str], ImageEntry]:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ManifestError(f"could not read harness manifest {path}: {exc}") from exc

    manifest: dict[tuple[str, str, str], ImageEntry] = {}
    for harness, commits in data.items():
        if not isinstance(commits, dict):
            raise ManifestError(f"harness {harness!r} must contain commit tables")
        for commit, commit_data in commits.items():
            if not _COMMIT_RE.fullmatch(commit):
                raise ManifestError(
                    f"invalid harness commit {commit!r}; expected 40 lowercase hex characters"
                )
            if not isinstance(commit_data, dict):
                raise ManifestError(f"commit {commit!r} must be a table")
            unknown_commit_keys = set(commit_data) - _COMMIT_KEYS
            if unknown_commit_keys:
                raise ManifestError(
                    f"unknown key(s) in commit table: {', '.join(sorted(unknown_commit_keys))}"
                )
            version = _expect_string(commit_data.get("version"), "version")
            withdrawn = commit_data.get("withdrawn")
            if not isinstance(withdrawn, bool):
                raise ManifestError("withdrawn must be a boolean")
            withdrawn_reason = commit_data.get("withdrawn_reason", "")
            if not isinstance(withdrawn_reason, str):
                raise ManifestError("withdrawn_reason must be a string")
            images = commit_data.get("images")
            if not isinstance(images, dict) or not images:
                raise ManifestError(f"commit {commit!r} must contain image architecture tables")
            for arch, image_data in images.items():
                if arch not in _ARCHES:
                    raise ManifestError(f"unsupported image architecture {arch!r}")
                if not isinstance(image_data, dict):
                    raise ManifestError(f"image entry for {arch!r} must be a table")
                unknown_image_keys = set(image_data) - _IMAGE_KEYS
                if unknown_image_keys:
                    raise ManifestError(
                        f"unknown key(s) in image table: {', '.join(sorted(unknown_image_keys))}"
                    )
                ref = _expect_string(image_data.get("ref"), "ref")
                digest = _expect_string(image_data.get("digest"), "digest")
                if not _DIGEST_RE.fullmatch(digest):
                    raise ManifestError(f"digest {digest!r} is not a valid sha256 digest")
                build_method = _expect_string(image_data.get("build_method"), "build_method")
                if build_method not in _BUILD_METHODS:
                    raise ManifestError("build_method must be 'source' or 'release-package'")
                built_at = _parse_date(image_data.get("built_at"))
                manifest[(harness, commit, arch)] = ImageEntry(
                    harness=harness,
                    commit=commit,
                    version=version,
                    arch=arch,
                    ref=ref,
                    digest=digest,
                    build_method=build_method,
                    built_at=built_at,
                    withdrawn=withdrawn,
                    withdrawn_reason=withdrawn_reason,
                )
    return manifest


def local_image_digest(
    ref: str,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> str | None:
    try:
        result = run(
            ["docker", "image", "inspect", "--format", "{{.Id}}", ref],
            capture_output=True,
            text=True,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    digest = result.stdout.strip()
    if result.returncode != 0 or not digest.startswith("sha256:"):
        return None
    return digest


def verify_arm(
    manifest: dict[tuple[str, str, str], ImageEntry],
    harness: str,
    commit: str,
    arch: str,
    digest_of: Callable[[str], str | None] = local_image_digest,
) -> ImageEntry:
    commit_entries = [
        entry
        for (entry_harness, entry_commit, _entry_arch), entry in manifest.items()
        if entry_harness == harness and entry_commit == commit
    ]
    if not commit_entries:
        raise ManifestError(f"commit {commit!r} is not listed for harness {harness!r}")
    entry = manifest.get((harness, commit, arch))
    if entry is None:
        raise ManifestError(
            f"commit {commit!r} is listed for harness {harness!r}, but not for architecture {arch!r}"
        )
    if entry.withdrawn:
        raise ManifestError(
            f"commit {commit!r} for harness {harness!r} is withdrawn: {entry.withdrawn_reason}"
        )
    local_digest = digest_of(entry.ref)
    if local_digest is None:
        raise ManifestError(f"local image {entry.ref!r} is missing")
    if local_digest != entry.digest:
        raise ManifestError(
            f"local image digest {local_digest!r} does not match manifest digest {entry.digest!r}"
        )
    return entry


def _quote(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _serialise(manifest: dict[tuple[str, str, str], ImageEntry]) -> str:
    grouped: dict[tuple[str, str], list[ImageEntry]] = {}
    for entry in manifest.values():
        grouped.setdefault((entry.harness, entry.commit), []).append(entry)
    tables: list[str] = []
    for harness, commit in sorted(grouped):
        entries = grouped[(harness, commit)]
        metadata = entries[0]
        tables.append(f"[{_quote(harness)}.{_quote(commit)}]")
        tables.append(f"version = {_quote(metadata.version)}")
        tables.append(f"withdrawn = {'true' if metadata.withdrawn else 'false'}")
        if metadata.withdrawn_reason:
            tables.append(f"withdrawn_reason = {_quote(metadata.withdrawn_reason)}")
        for entry in sorted(entries, key=lambda item: item.arch):
            tables.append("")
            tables.append(f"[{_quote(harness)}.{_quote(commit)}.images.{entry.arch}]")
            tables.append(f"ref = {_quote(entry.ref)}")
            tables.append(f"digest = {_quote(entry.digest)}")
            tables.append(f"build_method = {_quote(entry.build_method)}")
            tables.append(f"built_at = {_quote(entry.built_at)}")
        tables.append("")
    return "\n".join(tables)


def add_entry(path: Path, entry: ImageEntry) -> None:
    manifest = load_manifest(path) if path.exists() else {}
    commit_entries = [
        existing
        for (harness, commit, _arch), existing in manifest.items()
        if harness == entry.harness and commit == entry.commit
    ]
    if commit_entries:
        existing_metadata = commit_entries[0]
        if existing_metadata.version != entry.version:
            raise ManifestError(
                f"version {entry.version!r} differs from stored version "
                f"{existing_metadata.version!r} for commit {entry.commit!r}"
            )
        if any(existing.arch != entry.arch for existing in commit_entries):
            entry = ImageEntry(
                harness=entry.harness,
                commit=entry.commit,
                version=entry.version,
                arch=entry.arch,
                ref=entry.ref,
                digest=entry.digest,
                build_method=entry.build_method,
                built_at=entry.built_at,
                withdrawn=existing_metadata.withdrawn,
                withdrawn_reason=existing_metadata.withdrawn_reason,
            )
    manifest[(entry.harness, entry.commit, entry.arch)] = entry
    content = _serialise(manifest)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, path)
    except BaseException:
        with suppress(FileNotFoundError):
            os.unlink(temporary_name)
        raise


def host_arch(
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> str:
    try:
        result = run(
            ["docker", "info", "--format", "{{.Architecture}}"],
            capture_output=True,
            text=True,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ManifestError(f"could not determine Docker host architecture: {exc}") from exc
    if result.returncode != 0:
        raise ManifestError("could not determine Docker host architecture")
    architecture = result.stdout.strip().lower()
    if architecture in {"amd64", "x86_64", "x86-64"}:
        return "amd64"
    if architecture in {"arm64", "aarch64"}:
        return "arm64"
    raise ManifestError(f"unsupported Docker host architecture {architecture!r}")
