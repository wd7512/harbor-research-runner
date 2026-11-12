from __future__ import annotations

import subprocess
import tomllib
from pathlib import Path

import pytest

from harbor_research_runner.harness.manifest import (
    ImageEntry,
    ManifestError,
    add_entry,
    host_arch,
    load_manifest,
    local_image_digest,
    verify_arm,
)

COMMIT = "a" * 40
OTHER_COMMIT = "b" * 40
DIGEST = "sha256:" + "c" * 64
OTHER_DIGEST = "sha256:" + "d" * 64


def entry(
    *,
    commit: str = COMMIT,
    arch: str = "amd64",
    digest: str = DIGEST,
    version: str = "1.2.3",
    withdrawn: bool = False,
    withdrawn_reason: str = "",
    built_at: str = "2026-11-11",
) -> ImageEntry:
    return ImageEntry(
        harness="opencode",
        commit=commit,
        version=version,
        arch=arch,
        ref=f"hrr/opencode:{commit[:12]}-{arch}",
        digest=digest,
        build_method="source",
        built_at=built_at,
        withdrawn=withdrawn,
        withdrawn_reason=withdrawn_reason,
    )


def manifest_file(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "harness-manifest.toml"
    path.write_text(body, encoding="utf-8")
    return path


def test_manifest_refuses_unlisted_or_mismatched_image() -> None:
    cases = [
        ({}, "opencode", COMMIT, "amd64", DIGEST, "not listed"),
        (
            {("opencode", COMMIT, "arm64"): entry(arch="arm64")},
            "opencode",
            COMMIT,
            "amd64",
            DIGEST,
            "architecture",
        ),
        (
            {("opencode", COMMIT, "amd64"): entry(withdrawn=True, withdrawn_reason="bad build")},
            "opencode",
            COMMIT,
            "amd64",
            DIGEST,
            "bad build",
        ),
        (
            {("opencode", COMMIT, "amd64"): entry()},
            "opencode",
            COMMIT,
            "amd64",
            None,
            "missing",
        ),
        (
            {("opencode", COMMIT, "amd64"): entry()},
            "opencode",
            COMMIT,
            "amd64",
            OTHER_DIGEST,
            "digest",
        ),
    ]
    for manifest, harness, commit, arch, local_digest, message in cases:
        with pytest.raises(ManifestError, match=message):
            verify_arm(
                manifest,
                harness,
                commit,
                arch,
                digest_of=lambda _ref, value=local_digest: value,
            )


def test_verify_arm_allows_unwithdrawn_manifest_entry() -> None:
    available = entry()
    withdrawn = entry(
        commit=OTHER_COMMIT,
        withdrawn=True,
        withdrawn_reason="withdrawn commit",
    )
    manifest = {
        ("opencode", COMMIT, "amd64"): available,
        ("opencode", OTHER_COMMIT, "amd64"): withdrawn,
    }
    assert verify_arm(manifest, "opencode", COMMIT, "amd64", lambda _ref: DIGEST) == available


def test_add_entry_round_trips_and_preserves_other_entry(tmp_path: Path) -> None:
    path = tmp_path / "harness-manifest.toml"
    existing = entry(commit=OTHER_COMMIT, digest=OTHER_DIGEST)
    added = entry()
    add_entry(path, existing)
    add_entry(path, added)
    manifest = load_manifest(path)
    assert manifest[("opencode", OTHER_COMMIT, "amd64")] == existing
    assert manifest[("opencode", COMMIT, "amd64")] == added


def test_add_entry_round_trips_reason_with_double_quote(tmp_path: Path) -> None:
    path = tmp_path / "harness-manifest.toml"
    withdrawn = entry(withdrawn=True, withdrawn_reason='known "bad" build')
    add_entry(path, withdrawn)
    assert load_manifest(path)[("opencode", COMMIT, "amd64")] == withdrawn


def test_add_entry_round_trips_reason_with_backslash_newline_and_unicode(
    tmp_path: Path,
) -> None:
    path = tmp_path / "harness-manifest.toml"
    reason = "known \\ bad\nbuild café"
    add_entry(path, entry(withdrawn=True, withdrawn_reason=reason))
    parsed = tomllib.loads(path.read_text(encoding="utf-8"))
    assert parsed["opencode"][COMMIT]["withdrawn_reason"] == reason
    assert load_manifest(path)[("opencode", COMMIT, "amd64")].withdrawn_reason == reason


def test_add_entry_keeps_existing_commit_metadata_for_another_architecture(
    tmp_path: Path,
) -> None:
    path = tmp_path / "harness-manifest.toml"
    amd64 = entry(withdrawn=True, withdrawn_reason="withdrawn")
    arm64 = entry(
        arch="arm64",
        version=amd64.version,
        withdrawn=False,
        withdrawn_reason="",
    )
    add_entry(path, amd64)
    add_entry(path, arm64)
    manifest = load_manifest(path)
    assert manifest[("opencode", COMMIT, "amd64")] == amd64
    assert manifest[("opencode", COMMIT, "arm64")] == ImageEntry(
        **{**arm64.__dict__, "withdrawn": True, "withdrawn_reason": "withdrawn"}
    )


def test_add_entry_rejects_version_change_for_existing_commit(tmp_path: Path) -> None:
    path = tmp_path / "harness-manifest.toml"
    add_entry(path, entry())
    with pytest.raises(ManifestError, match="version"):
        add_entry(path, entry(arch="arm64", version="9.9.9"))


def test_add_entry_output_is_valid_toml(tmp_path: Path) -> None:
    path = tmp_path / "harness-manifest.toml"
    add_entry(path, entry())
    parsed = tomllib.loads(path.read_text(encoding="utf-8"))
    assert parsed["opencode"][COMMIT]["images"]["amd64"]["built_at"] == "2026-11-11"


@pytest.mark.parametrize("built_at", [None, "2026/11/11", "2026-02-30"])
def test_load_manifest_rejects_missing_or_malformed_built_at(
    tmp_path: Path, built_at: str | None
) -> None:
    field = "" if built_at is None else f'built_at = "{built_at}"\n'
    path = manifest_file(
        tmp_path,
        f'[opencode."{COMMIT}"]\nversion = "1.2.3"\nwithdrawn = false\n'
        f'[opencode."{COMMIT}".images.amd64]\nref = "hrr/opencode:test"\n'
        f'digest = "{DIGEST}"\nbuild_method = "source"\n{field}',
    )
    with pytest.raises(ManifestError, match="built_at"):
        load_manifest(path)


def test_load_manifest_rejects_unknown_commit_key(tmp_path: Path) -> None:
    path = manifest_file(
        tmp_path,
        f'[opencode."{COMMIT}"]\nversion = "1.2.3"\nwithdrawn = false\nother = "x"\n'
        f'[opencode."{COMMIT}".images.amd64]\nref = "r"\ndigest = "{DIGEST}"\n'
        'build_method = "source"\nbuilt_at = "2026-11-11"\n',
    )
    with pytest.raises(ManifestError, match="unknown key"):
        load_manifest(path)


def test_load_manifest_rejects_unknown_image_key(tmp_path: Path) -> None:
    path = manifest_file(
        tmp_path,
        f'[opencode."{COMMIT}"]\nversion = "1.2.3"\nwithdrawn = false\n'
        f'[opencode."{COMMIT}".images.amd64]\nref = "r"\ndigest = "{DIGEST}"\n'
        'build_method = "source"\nbuilt_at = "2026-11-11"\nother = "x"\n',
    )
    with pytest.raises(ManifestError, match="unknown key"):
        load_manifest(path)


@pytest.mark.parametrize("digest", ["invalid", "sha256:" + "a" * 63, "sha256:" + "g" * 64])
def test_load_manifest_rejects_malformed_digest(tmp_path: Path, digest: str) -> None:
    path = manifest_file(
        tmp_path,
        f'[opencode."{COMMIT}"]\nversion = "1.2.3"\nwithdrawn = false\n'
        f'[opencode."{COMMIT}".images.amd64]\nref = "r"\ndigest = "{digest}"\n'
        'build_method = "source"\nbuilt_at = "2026-11-11"\n',
    )
    with pytest.raises(ManifestError, match="digest"):
        load_manifest(path)


def test_load_manifest_rejects_missing_digest(tmp_path: Path) -> None:
    path = manifest_file(
        tmp_path,
        f'[opencode."{COMMIT}"]\nversion = "1.2.3"\nwithdrawn = false\n'
        f'[opencode."{COMMIT}".images.amd64]\nref = "r"\n'
        'build_method = "source"\nbuilt_at = "2026-11-11"\n',
    )
    with pytest.raises(ManifestError, match="digest"):
        load_manifest(path)


def test_load_manifest_rejects_non_commit_key(tmp_path: Path) -> None:
    path = manifest_file(
        tmp_path,
        '[opencode."1234567"]\nversion = "1.2.3"\nwithdrawn = false\n'
        '[opencode."1234567".images.amd64]\nref = "r"\n'
        f'digest = "{DIGEST}"\nbuild_method = "source"\nbuilt_at = "2026-11-11"\n',
    )
    with pytest.raises(ManifestError, match="40 lowercase hex"):
        load_manifest(path)


def test_load_manifest_preserves_hash_in_quoted_value(tmp_path: Path) -> None:
    path = manifest_file(
        tmp_path,
        f'[opencode."{COMMIT}"]\nversion = "1.2.3"\nwithdrawn = false\n'
        f'[opencode."{COMMIT}".images.amd64]\nref = "hrr/opencode:tag#2"\n'
        f'digest = "{DIGEST}"\nbuild_method = "source"\nbuilt_at = "2026-11-11"\n',
    )
    assert load_manifest(path)[("opencode", COMMIT, "amd64")].ref == "hrr/opencode:tag#2"


def test_load_manifest_honors_comments(tmp_path: Path) -> None:
    path = manifest_file(
        tmp_path,
        f'# manifest comment\n[opencode."{COMMIT}"]\nversion = "1.2.3" # version\n'
        f'withdrawn = false\n[opencode."{COMMIT}".images.amd64]\nref = "r"\n'
        f'digest = "{DIGEST}"\nbuild_method = "source"\nbuilt_at = "2026-11-11"\n',
    )
    assert load_manifest(path)[("opencode", COMMIT, "amd64")].version == "1.2.3"


def test_load_manifest_rejects_malformed_toml(tmp_path: Path) -> None:
    path = manifest_file(tmp_path, "[[[\n")
    with pytest.raises(ManifestError, match="could not read harness manifest"):
        load_manifest(path)


def test_load_manifest_rejects_duplicate_commit_table(tmp_path: Path) -> None:
    path = manifest_file(
        tmp_path,
        f'[opencode."{COMMIT}"]\nversion = "1.2.3"\nwithdrawn = false\n'
        f'[opencode."{COMMIT}"]\nversion = "1.2.3"\nwithdrawn = false\n',
    )
    with pytest.raises(ManifestError, match="could not read harness manifest"):
        load_manifest(path)


def test_local_image_digest_returns_none_when_absent() -> None:
    def absent(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, 1, stdout="", stderr="missing")

    assert local_image_digest("some-image", run=absent) is None


def test_local_image_digest_returns_image_id() -> None:
    def present(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, 0, stdout=DIGEST + "\n", stderr="")

    assert local_image_digest("some-image", run=present) == DIGEST


@pytest.mark.parametrize(("reported", "expected"), [("x86_64", "amd64"), ("aarch64", "arm64")])
def test_host_arch_maps_docker_architecture(reported: str, expected: str) -> None:
    def info(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, 0, stdout=reported + "\n", stderr="")

    assert host_arch(run=info) == expected


def test_host_arch_refuses_unknown_architecture() -> None:
    def info(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, 0, stdout="mips\n", stderr="")

    with pytest.raises(ManifestError, match="architecture"):
        host_arch(run=info)
