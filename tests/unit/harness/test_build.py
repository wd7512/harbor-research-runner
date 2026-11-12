from __future__ import annotations

import subprocess
from datetime import date
from pathlib import Path

import pytest

import harbor_research_runner.harness.build as build_module
from harbor_research_runner.harness.build import (
    BuildError,
    build,
    build_command,
    layout_for,
)
from harbor_research_runner.harness.manifest import ImageEntry, load_manifest

COMMIT = "810c0eba1a7348036f8bdf61e6632d0c36c6876c"
COMMIT_2X = "fedc6f208aa2bd54c1019227214568083f6380ad"
OTHER_COMMIT = "d60393835c5c40351d804f0e94013922c34b38bf"
DIGEST = "sha256:" + "a" * 64
OTHER_DIGEST = "sha256:" + "b" * 64
WITHDRAWN_REASON = 'known "bad" build'
AMD64_NODE = (
    "node:22-bookworm-slim@sha256:ea11b74b986d520137cdef9a5346e447a00f275ecc4abc0055b7151226231e09"
)
ARM64_NODE = (
    "node:22-bookworm-slim@sha256:a09c611805f0d53d16ca876bd550ec9a4ae0bce65036d270659892d068f59a7c"
)
LABEL_FORMAT = '{{index .Config.Labels "hrr.opencode.commit"}}'
BUILD_DIR = Path(build_module.__file__).parent / "build"
LISTING_FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "fixtures"
    / "opencode_build"
    / "opencode-1.19.5-listing.txt"
)


def image_ref(commit: str, arch: str) -> str:
    return f"hrr/opencode:{commit[:12]}-{arch}"


class FakeRun:
    def __init__(
        self,
        *,
        docker_arch: str = "aarch64",
        listing: list[str] | None = None,
        image_id: str = DIGEST,
        image_id_exit: int = 0,
        image_arch: str = "arm64",
        image_os: str = "linux",
        label: str = COMMIT,
        version: str = "1.19.5\n",
        version_exit: int = 0,
        build_exit: int = 0,
    ) -> None:
        self.docker_arch = docker_arch
        self.listing = ["packages/opencode/script/build.ts"] if listing is None else listing
        self.image_id = image_id
        self.image_id_exit = image_id_exit
        self.image_arch = image_arch
        self.image_os = image_os
        self.label = label
        self.version = version
        self.version_exit = version_exit
        self.build_exit = build_exit
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        args = list(args)
        self.calls.append(args)
        if args[:2] == ["docker", "info"]:
            return subprocess.CompletedProcess(args, 0, stdout=self.docker_arch + "\n", stderr="")
        if args[0] == "curl":
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        if args[0] == "tar":
            return subprocess.CompletedProcess(
                args, 0, stdout="\n".join(self.listing) + "\n", stderr=""
            )
        if args[:2] == ["docker", "build"]:
            return subprocess.CompletedProcess(
                args,
                self.build_exit,
                stdout="",
                stderr="build failed" if self.build_exit else "",
            )
        if args[:3] == ["docker", "image", "inspect"]:
            template = args[4]
            if template == "{{.Id}}":
                return subprocess.CompletedProcess(
                    args, self.image_id_exit, stdout=self.image_id + "\n", stderr=""
                )
            if template == "{{.Architecture}}":
                value = self.image_arch
            elif template == "{{.Os}}":
                value = self.image_os
            elif template == LABEL_FORMAT:
                value = self.label
            else:
                raise AssertionError(f"unexpected inspect template {template!r}")
            return subprocess.CompletedProcess(args, 0, stdout=value + "\n", stderr="")
        if args[:2] == ["docker", "run"]:
            return subprocess.CompletedProcess(
                args, self.version_exit, stdout=self.version, stderr=""
            )
        raise AssertionError(f"unexpected command {args!r}")


def archive_path(run: FakeRun) -> Path:
    curl = next(call for call in run.calls if call[0] == "curl")
    return Path(curl[4])


def inspect_calls(run: FakeRun) -> list[list[str]]:
    return [call for call in run.calls if call[:3] == ["docker", "image", "inspect"]]


def write_withdrawn_manifest(path: Path, *, arch: str, version: str = "1.19.5") -> None:
    path.write_text(
        f'[opencode."{COMMIT}"]\n'
        f'version = "{version}"\n'
        f"withdrawn = true\n"
        f"withdrawn_reason = '{WITHDRAWN_REASON}'\n"
        f"\n"
        f'[opencode."{COMMIT}".images.{arch}]\n'
        f'ref = "{image_ref(COMMIT, arch)}"\n'
        f'digest = "{DIGEST}"\n'
        f'build_method = "source"\n'
        f'built_at = "2026-11-01"\n'
        f"\n"
        f'[opencode."{OTHER_COMMIT}"]\n'
        f'version = "1.0.224"\n'
        f"withdrawn = false\n"
        f"\n"
        f'[opencode."{OTHER_COMMIT}".images.amd64]\n'
        f'ref = "{image_ref(OTHER_COMMIT, "amd64")}"\n'
        f'digest = "{OTHER_DIGEST}"\n'
        f'build_method = "source"\n'
        f'built_at = "2026-11-02"\n',
        encoding="utf-8",
    )


def test_layout_for_accepts_bare_v1_marker() -> None:
    assert layout_for(["package.json", "packages/opencode/script/build.ts"]) == "v1"


def test_layout_for_accepts_v2_marker_under_one_archive_directory() -> None:
    assert (
        layout_for(
            [
                "opencode-abc123/package.json",
                "opencode-abc123/packages/cli/script/build.ts",
            ]
        )
        == "v2"
    )


def test_layout_for_rejects_listing_without_supported_marker() -> None:
    with pytest.raises(BuildError, match="layout"):
        layout_for(["opencode-abc123/packages/other/script/build.ts"])


def test_layout_for_selects_v1_for_the_real_mixed_1_19_archive() -> None:
    assert layout_for(LISTING_FIXTURE.read_text(encoding="utf-8").splitlines()) == "v1"


@pytest.mark.parametrize(
    "listing",
    [
        [
            "packages/opencode/script/build.ts",
            "opencode-abc123/packages/opencode/script/build.ts",
        ],
        [
            "packages/cli/script/build.ts",
            "opencode-abc123/packages/cli/script/build.ts",
        ],
    ],
)
def test_layout_for_rejects_duplicate_marker(listing: list[str]) -> None:
    with pytest.raises(BuildError, match="ambiguous"):
        layout_for(listing)


def test_build_command_for_v1_targets_arm64_with_the_pinned_base() -> None:
    assert build_command("opencode", COMMIT, "arm64", "v1") == [
        "docker",
        "build",
        "--platform",
        "linux/arm64",
        "--build-arg",
        f"NODE_BASE={ARM64_NODE}",
        "--build-arg",
        f"OPENCODE_COMMIT={COMMIT}",
        "-t",
        image_ref(COMMIT, "arm64"),
        "-f",
        str(BUILD_DIR / "Dockerfile.source"),
        str(BUILD_DIR),
    ]


def test_build_command_for_v2_targets_amd64_with_the_two_x_recipe() -> None:
    assert build_command("opencode", COMMIT_2X, "amd64", "v2") == [
        "docker",
        "build",
        "--platform",
        "linux/amd64",
        "--build-arg",
        f"NODE_BASE={AMD64_NODE}",
        "--build-arg",
        f"OPENCODE_COMMIT={COMMIT_2X}",
        "-t",
        image_ref(COMMIT_2X, "amd64"),
        "-f",
        str(BUILD_DIR / "Dockerfile.source-2x"),
        str(BUILD_DIR),
    ]


def test_build_command_rejects_unknown_layout() -> None:
    with pytest.raises(BuildError, match="layout"):
        build_command("opencode", COMMIT, "amd64", "v3")


def test_build_command_rejects_unsupported_architecture() -> None:
    with pytest.raises(BuildError, match="architecture"):
        build_command("opencode", COMMIT, "riscv64", "v1")


def test_build_rejects_unknown_harness_without_running_commands(tmp_path: Path) -> None:
    run = FakeRun()
    with pytest.raises(BuildError, match="opencode"):
        build("codex", COMMIT, tmp_path / "harness-manifest.toml", run=run)
    assert run.calls == []


@pytest.mark.parametrize("commit", ["A" * 40, "a" * 39, "g" * 40])
def test_build_rejects_invalid_commit_without_running_commands(tmp_path: Path, commit: str) -> None:
    run = FakeRun()
    with pytest.raises(BuildError, match="commit"):
        build("opencode", commit, tmp_path / "harness-manifest.toml", run=run)
    assert run.calls == []


def test_build_rejects_unsupported_docker_architecture(tmp_path: Path) -> None:
    run = FakeRun(docker_arch="mips")
    with pytest.raises(BuildError, match="architecture"):
        build("opencode", COMMIT, tmp_path / "harness-manifest.toml", run=run)
    assert run.calls == [["docker", "info", "--format", "{{.Architecture}}"]]


def test_build_rejects_unknown_source_layout(tmp_path: Path) -> None:
    run = FakeRun(listing=["opencode-abc123/packages/other/script/build.ts"])
    with pytest.raises(BuildError, match="layout"):
        build("opencode", COMMIT, tmp_path / "harness-manifest.toml", run=run)
    assert not any(call[:2] == ["docker", "build"] for call in run.calls)


def test_build_rejects_duplicate_source_marker(tmp_path: Path) -> None:
    run = FakeRun(
        listing=[
            "packages/opencode/script/build.ts",
            "opencode-abc123/packages/opencode/script/build.ts",
        ]
    )
    with pytest.raises(BuildError, match="ambiguous"):
        build("opencode", COMMIT, tmp_path / "harness-manifest.toml", run=run)
    assert not any(call[:2] == ["docker", "build"] for call in run.calls)


def test_build_downloads_the_commit_archive_and_runs_the_platform_build(tmp_path: Path) -> None:
    run = FakeRun(docker_arch="aarch64")
    build("opencode", COMMIT, tmp_path / "harness-manifest.toml", run=run)

    assert run.calls[0] == ["docker", "info", "--format", "{{.Architecture}}"]
    curl = next(call for call in run.calls if call[0] == "curl")
    assert curl[:4] == ["curl", "--fail", "--location", "--output"]
    assert Path(curl[4]).name == "source.tar.gz"
    assert curl[5] == f"https://github.com/anomalyco/opencode/archive/{COMMIT}.tar.gz"
    assert ["tar", "-tzf", curl[4]] in run.calls
    assert build_command("opencode", COMMIT, "arm64", "v1") in run.calls


def test_build_inspects_the_built_image_identity_and_runs_version(tmp_path: Path) -> None:
    run = FakeRun(image_arch="arm64")
    build("opencode", COMMIT, tmp_path / "harness-manifest.toml", run=run)
    ref = image_ref(COMMIT, "arm64")

    assert inspect_calls(run) == [
        ["docker", "image", "inspect", "--format", "{{.Id}}", ref],
        ["docker", "image", "inspect", "--format", "{{.Architecture}}", ref],
        ["docker", "image", "inspect", "--format", "{{.Os}}", ref],
        ["docker", "image", "inspect", "--format", LABEL_FORMAT, ref],
    ]
    assert ["docker", "run", "--rm", "--platform", "linux/arm64", ref, "opencode", "--version"] in (
        run.calls
    )


def test_build_with_real_mixed_listing_uses_the_v1_recipe(tmp_path: Path) -> None:
    listing = LISTING_FIXTURE.read_text(encoding="utf-8").splitlines()
    run = FakeRun(listing=listing, version="1.19.5\n")

    entry = build("opencode", COMMIT, tmp_path / "harness-manifest.toml", run=run)

    build_call = next(call for call in run.calls if call[:2] == ["docker", "build"])
    assert build_call == build_command("opencode", COMMIT, "arm64", "v1")
    assert entry.ref == image_ref(COMMIT, "arm64")
    assert entry.version == "1.19.5"


def test_build_writes_one_manifest_entry_with_the_image_digest(tmp_path: Path) -> None:
    path = tmp_path / "harness-manifest.toml"
    run = FakeRun()
    entry = build("opencode", COMMIT, path, run=run)

    assert entry == ImageEntry(
        harness="opencode",
        commit=COMMIT,
        version="1.19.5",
        arch="arm64",
        ref=image_ref(COMMIT, "arm64"),
        digest=DIGEST,
        build_method="source",
        built_at=date.today().isoformat(),
        withdrawn=False,
        withdrawn_reason="",
    )
    assert load_manifest(path) == {("opencode", COMMIT, "arm64"): entry}


def test_build_preserves_withdrawal_on_same_architecture_rebuild(tmp_path: Path) -> None:
    path = tmp_path / "harness-manifest.toml"
    write_withdrawn_manifest(path, arch="arm64")
    before = load_manifest(path)
    run = FakeRun(docker_arch="aarch64", version="1.19.5\n")

    entry = build("opencode", COMMIT, path, run=run)
    after = load_manifest(path)

    assert entry.withdrawn is True
    assert entry.withdrawn_reason == WITHDRAWN_REASON
    assert after[("opencode", COMMIT, "arm64")] == entry
    assert after[("opencode", OTHER_COMMIT, "amd64")] == before[("opencode", OTHER_COMMIT, "amd64")]


def test_build_preserves_withdrawal_when_adding_another_architecture(tmp_path: Path) -> None:
    path = tmp_path / "harness-manifest.toml"
    write_withdrawn_manifest(path, arch="arm64")
    before = load_manifest(path)
    run = FakeRun(docker_arch="x86_64", image_arch="amd64", version="1.19.5\n")

    entry = build("opencode", COMMIT, path, run=run)
    after = load_manifest(path)

    assert entry.withdrawn is True
    assert entry.withdrawn_reason == WITHDRAWN_REASON
    assert after[("opencode", COMMIT, "amd64")] == entry
    assert after[("opencode", COMMIT, "arm64")] == before[("opencode", COMMIT, "arm64")]
    assert after[("opencode", OTHER_COMMIT, "amd64")] == before[("opencode", OTHER_COMMIT, "amd64")]


def test_build_parses_the_two_x_version_banner(tmp_path: Path) -> None:
    run = FakeRun(
        listing=["packages/cli/script/build.ts"],
        version="opencode v2.2.7\n",
        label=COMMIT_2X,
    )
    entry = build("opencode", COMMIT_2X, tmp_path / "harness-manifest.toml", run=run)
    assert entry.version == "2.2.7"
    assert entry.ref == image_ref(COMMIT_2X, "arm64")


@pytest.mark.parametrize(
    ("banner", "expected"),
    [
        ("1.0.224\n", "1.0.224"),
        ("1.14.51\n", "1.14.51"),
        ("1.19.5\n", "1.19.5"),
        ("opencode v2.2.7\n", "2.2.7"),
    ],
)
def test_build_keeps_stable_source_sweep_versions(
    tmp_path: Path, banner: str, expected: str
) -> None:
    run = FakeRun(version=banner)
    entry = build("opencode", COMMIT, tmp_path / "harness-manifest.toml", run=run)
    assert entry.version == expected


@pytest.mark.parametrize(
    ("commit", "listing", "banner", "expected"),
    [
        (COMMIT, None, "1.20.0-dev.3+abc123\n", "1.20.0-dev.3+abc123"),
        (
            COMMIT_2X,
            ["packages/cli/script/build.ts"],
            "opencode v2.3.0-rc.1\n",
            "2.3.0-rc.1",
        ),
    ],
)
def test_build_preserves_prerelease_and_build_suffixes(
    tmp_path: Path,
    commit: str,
    listing: list[str] | None,
    banner: str,
    expected: str,
) -> None:
    run = FakeRun(listing=listing, version=banner, label=commit)
    entry = build("opencode", commit, tmp_path / "harness-manifest.toml", run=run)
    assert entry.version == expected


def test_build_rejects_image_architecture_mismatch(tmp_path: Path) -> None:
    path = tmp_path / "harness-manifest.toml"
    path.write_text("original manifest bytes\n", encoding="utf-8")
    run = FakeRun(docker_arch="aarch64", image_arch="amd64")
    with pytest.raises(BuildError, match="architecture"):
        build("opencode", COMMIT, path, run=run)
    assert path.read_text(encoding="utf-8") == "original manifest bytes\n"


def test_build_rejects_non_linux_image(tmp_path: Path) -> None:
    path = tmp_path / "harness-manifest.toml"
    path.write_text("original manifest bytes\n", encoding="utf-8")
    run = FakeRun(image_os="windows")
    with pytest.raises(BuildError, match="linux"):
        build("opencode", COMMIT, path, run=run)
    assert path.read_text(encoding="utf-8") == "original manifest bytes\n"


def test_build_rejects_missing_image_digest(tmp_path: Path) -> None:
    path = tmp_path / "harness-manifest.toml"
    path.write_text("original manifest bytes\n", encoding="utf-8")
    run = FakeRun(image_id_exit=1, image_id="")
    with pytest.raises(BuildError, match="image id"):
        build("opencode", COMMIT, path, run=run)
    assert path.read_text(encoding="utf-8") == "original manifest bytes\n"


def test_build_rejects_absent_label(tmp_path: Path) -> None:
    path = tmp_path / "harness-manifest.toml"
    path.write_text("original manifest bytes\n", encoding="utf-8")
    run = FakeRun(label="<no value>")
    with pytest.raises(BuildError, match="label"):
        build("opencode", COMMIT, path, run=run)
    assert path.read_text(encoding="utf-8") == "original manifest bytes\n"


def test_build_rejects_mismatched_label(tmp_path: Path) -> None:
    path = tmp_path / "harness-manifest.toml"
    path.write_text("original manifest bytes\n", encoding="utf-8")
    run = FakeRun(label=COMMIT_2X)
    with pytest.raises(BuildError, match="label"):
        build("opencode", COMMIT, path, run=run)
    assert path.read_text(encoding="utf-8") == "original manifest bytes\n"


def test_build_rejects_empty_version(tmp_path: Path) -> None:
    path = tmp_path / "harness-manifest.toml"
    path.write_text("original manifest bytes\n", encoding="utf-8")
    run = FakeRun(version="\n")
    with pytest.raises(BuildError, match="version"):
        build("opencode", COMMIT, path, run=run)
    assert path.read_text(encoding="utf-8") == "original manifest bytes\n"


def test_build_rejects_unparseable_version(tmp_path: Path) -> None:
    path = tmp_path / "harness-manifest.toml"
    path.write_text("original manifest bytes\n", encoding="utf-8")
    run = FakeRun(version="opencode vdev\n")
    with pytest.raises(BuildError, match="version"):
        build("opencode", COMMIT, path, run=run)
    assert path.read_text(encoding="utf-8") == "original manifest bytes\n"


@pytest.mark.parametrize(
    "banner",
    [
        "1.20.0-\n",
        "1.20.0+\n",
        "1.20.0-dev..3\n",
        "1.20.0+build..1\n",
        "opencode v2.3.0-rc.\n",
    ],
)
def test_build_rejects_malformed_version_suffix(tmp_path: Path, banner: str) -> None:
    path = tmp_path / "harness-manifest.toml"
    path.write_text("original manifest bytes\n", encoding="utf-8")
    run = FakeRun(version=banner)
    with pytest.raises(BuildError, match="version"):
        build("opencode", COMMIT, path, run=run)
    assert path.read_text(encoding="utf-8") == "original manifest bytes\n"


def test_build_rejects_failed_version_command(tmp_path: Path) -> None:
    path = tmp_path / "harness-manifest.toml"
    path.write_text("original manifest bytes\n", encoding="utf-8")
    run = FakeRun(version_exit=1)
    with pytest.raises(BuildError, match="version"):
        build("opencode", COMMIT, path, run=run)
    assert path.read_text(encoding="utf-8") == "original manifest bytes\n"


def test_build_rejects_failed_docker_build(tmp_path: Path) -> None:
    path = tmp_path / "harness-manifest.toml"
    path.write_text("original manifest bytes\n", encoding="utf-8")
    run = FakeRun(build_exit=1)
    with pytest.raises(BuildError, match="build"):
        build("opencode", COMMIT, path, run=run)
    assert path.read_text(encoding="utf-8") == "original manifest bytes\n"


def test_build_recipes_ship_as_package_data() -> None:
    for name in ("Dockerfile.source", "Dockerfile.source-2x"):
        text = (BUILD_DIR / name).read_text(encoding="utf-8")
        assert "FROM ${NODE_BASE}" in text
        assert 'LABEL hrr.opencode.commit="${OPENCODE_COMMIT}"' in text
        assert "custom-harbor" not in text
