"""Build a harness image from an upstream commit and record it in the manifest."""

from __future__ import annotations

import re
import subprocess
import tempfile
from collections.abc import Callable
from datetime import date
from pathlib import Path

from harbor_research_runner.harness.manifest import (
    ImageEntry,
    ManifestError,
    add_entry,
    host_arch,
    load_manifest,
    local_image_digest,
)

ExternalRunner = Callable[..., subprocess.CompletedProcess[str]]


class BuildError(RuntimeError):
    """Raised when a harness image cannot be built or proven."""


_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_VERSION_RE = re.compile(
    r"^(?:opencode v)?"
    r"(\d+\.\d+\.\d+"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?)$"
)
_MARKERS = {
    "v1": "packages/opencode/script/build.ts",
    "v2": "packages/cli/script/build.ts",
}
_DOCKERFILES = {
    "v1": "Dockerfile.source",
    "v2": "Dockerfile.source-2x",
}
_NODE_BASES = {
    "amd64": "node:22-bookworm-slim@sha256:ea11b74b986d520137cdef9a5346e447a00f275ecc4abc0055b7151226231e09",
    "arm64": "node:22-bookworm-slim@sha256:a09c611805f0d53d16ca876bd550ec9a4ae0bce65036d270659892d068f59a7c",
}
_LABEL_FORMAT = '{{index .Config.Labels "hrr.opencode.commit"}}'
_SOURCE_URL = "https://github.com/anomalyco/opencode/archive/{commit}.tar.gz"


def _build_dir() -> Path:
    return Path(__file__).with_name("build")


def _require_harness(harness: str) -> None:
    if harness != "opencode":
        raise BuildError(f"unsupported harness {harness!r}; only 'opencode' is accepted")


def _require_commit(commit: str) -> None:
    if not _COMMIT_RE.fullmatch(commit):
        raise BuildError(f"invalid commit {commit!r}; expected 40 lowercase hexadecimal characters")


def _names_marker(line: str, marker: str) -> bool:
    if line == marker:
        return True
    prefix, separator, rest = line.partition("/")
    return bool(separator and prefix and rest == marker)


def layout_for(commit_tree_listing: list[str]) -> str:
    """Return the supported layout for an archive listing, preferring the proven v1 marker."""
    counts = {
        layout: sum(1 for line in commit_tree_listing if _names_marker(line, marker))
        for layout, marker in _MARKERS.items()
    }
    if counts["v1"] > 1 or counts["v2"] > 1:
        raise BuildError("ambiguous source layout: duplicate supported build marker")
    if counts["v1"] == 1:
        return "v1"
    if counts["v2"] == 1:
        return "v2"
    raise BuildError("unsupported source layout: no supported build marker found")


def _image_ref(commit: str, arch: str) -> str:
    return f"hrr/opencode:{commit[:12]}-{arch}"


def build_command(harness: str, commit: str, arch: str, layout: str) -> list[str]:
    """Return the ``docker build`` command for one commit, architecture and layout."""
    _require_harness(harness)
    _require_commit(commit)
    if arch not in _NODE_BASES:
        raise BuildError(f"unsupported architecture {arch!r}; expected 'amd64' or 'arm64'")
    if layout not in _DOCKERFILES:
        raise BuildError(f"unsupported layout {layout!r}; expected 'v1' or 'v2'")
    build_dir = _build_dir()
    return [
        "docker",
        "build",
        "--platform",
        f"linux/{arch}",
        "--build-arg",
        f"NODE_BASE={_NODE_BASES[arch]}",
        "--build-arg",
        f"OPENCODE_COMMIT={commit}",
        "-t",
        _image_ref(commit, arch),
        "-f",
        str(build_dir / _DOCKERFILES[layout]),
        str(build_dir),
    ]


def _run(args: list[str], run: ExternalRunner) -> subprocess.CompletedProcess[str]:
    try:
        return run(args, capture_output=True, text=True, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise BuildError(f"could not run {args[0]!r}: {exc}") from exc


def resolve_arch(arch: str | None, run: ExternalRunner = subprocess.run) -> str:
    if arch is not None:
        if arch not in _NODE_BASES:
            raise BuildError(f"unsupported architecture {arch!r}; expected 'amd64' or 'arm64'")
        return arch
    try:
        return host_arch(run=run)
    except ManifestError as exc:
        raise BuildError(str(exc)) from exc


def _archive_listing(archive: Path, run: ExternalRunner) -> list[str]:
    result = _run(["tar", "-tzf", str(archive)], run)
    if result.returncode != 0:
        raise BuildError(
            f"could not list source archive {archive} (exit {result.returncode}): "
            f"{result.stderr.strip()}"
        )
    return result.stdout.splitlines()


def _inspect(ref: str, template: str, run: ExternalRunner) -> str:
    result = _run(["docker", "image", "inspect", "--format", template, ref], run)
    if result.returncode != 0:
        raise BuildError(
            f"could not inspect image {ref!r} (exit {result.returncode}): {result.stderr.strip()}"
        )
    return result.stdout.strip()


def _prove_image(ref: str, commit: str, arch: str, run: ExternalRunner) -> str:
    digest = local_image_digest(ref, run=run)
    if digest is None or not _DIGEST_RE.fullmatch(digest):
        raise BuildError(f"could not read a valid local image id for {ref!r}")

    image_arch = _inspect(ref, "{{.Architecture}}", run)
    if image_arch != arch:
        raise BuildError(f"image {ref!r} has architecture {image_arch!r}, expected {arch!r}")

    image_os = _inspect(ref, "{{.Os}}", run)
    if image_os != "linux":
        raise BuildError(f"image {ref!r} has OS {image_os!r}, expected 'linux'")

    label = _inspect(ref, _LABEL_FORMAT, run)
    if label != commit:
        raise BuildError(f"image {ref!r} has commit label {label!r}, expected {commit!r}")

    return digest


def _version_from_image(ref: str, arch: str, run: ExternalRunner) -> str:
    result = _run(
        ["docker", "run", "--rm", "--platform", f"linux/{arch}", ref, "opencode", "--version"],
        run,
    )
    output = result.stdout.strip()
    if result.returncode != 0:
        raise BuildError(
            f"opencode --version failed in image {ref!r} (exit {result.returncode}): "
            f"{result.stderr.strip()}"
        )
    match = _VERSION_RE.fullmatch(output)
    if match is None:
        raise BuildError(f"could not parse opencode version from image output {output!r}")
    return match.group(1)


def _existing_commit_entry(manifest_path: Path, harness: str, commit: str) -> ImageEntry | None:
    if not manifest_path.exists():
        return None
    for (entry_harness, entry_commit, _arch), entry in load_manifest(manifest_path).items():
        if entry_harness == harness and entry_commit == commit:
            return entry
    return None


def build(
    harness: str,
    commit: str,
    manifest_path: Path,
    arch: str,
    run: ExternalRunner = subprocess.run,
) -> ImageEntry:
    """Build one harness image from source and add it to the commit manifest."""
    _require_harness(harness)
    _require_commit(commit)
    with tempfile.TemporaryDirectory(prefix="hrr-opencode-source-") as directory:
        archive = Path(directory) / "source.tar.gz"
        url = _SOURCE_URL.format(commit=commit)
        download = _run(["curl", "--fail", "--location", "--output", str(archive), url], run)
        if download.returncode != 0:
            raise BuildError(
                f"could not download source archive for commit {commit!r} "
                f"(exit {download.returncode}): {download.stderr.strip()}"
            )
        layout = layout_for(_archive_listing(archive, run))

        result = _run(build_command(harness, commit, arch, layout), run)
        if result.returncode != 0:
            raise BuildError(
                f"docker build failed for commit {commit!r} (exit {result.returncode}): "
                f"{result.stderr.strip()}"
            )

    ref = _image_ref(commit, arch)
    digest = _prove_image(ref, commit, arch, run)
    version = _version_from_image(ref, arch, run)
    existing = _existing_commit_entry(manifest_path, harness, commit)
    entry = ImageEntry(
        harness=harness,
        commit=commit,
        version=version,
        arch=arch,
        ref=ref,
        digest=digest,
        build_method="source",
        built_at=date.today().isoformat(),
        withdrawn=existing.withdrawn if existing is not None else False,
        withdrawn_reason=existing.withdrawn_reason if existing is not None else "",
    )
    add_entry(manifest_path, entry)
    return entry
