from pathlib import Path

import pytest

from harbor_research_runner.job_config.job_file import JobFileError, load_job, planned_slots


def write_job(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "job.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def valid_job() -> str:
    return """name: study
model: opencodego/mimo-v2.6-pro
repetitions: 2
concurrency: 1
task_order: as_listed
tasks:
  dataset: example/data
  include: [one, two]
  exclude: []
machine_check: checks/machine.json
arms:
  - {label: old, harness: opencode, commit: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa}
proxy: {host: 127.0.0.1, port: 8766, container_host: host.docker.internal}
limits: {}
"""


@pytest.mark.parametrize(
    "text",
    [
        valid_job().replace("limits: {}", "limits: {}\nunknown: true"),
        valid_job().replace("  dataset: example/data", "  dataset: example/data\n  extra: true"),
        valid_job().replace("label: old", "label: old, extra: true"),
        valid_job().replace("host: 127.0.0.1", "host: 127.0.0.1, extra: true"),
    ],
    ids=["top-level", "tasks", "arm", "proxy"],
)
def test_rejects_unknown_job_keys(tmp_path: Path, text: str) -> None:
    with pytest.raises(JobFileError, match="unknown"):
        load_job(write_job(tmp_path, text))


@pytest.mark.parametrize("field", ["repetitions", "concurrency"])
@pytest.mark.parametrize("value", [0, -1, True, 1.5, "2"])
def test_rejects_invalid_positive_integer(tmp_path: Path, field: str, value: object) -> None:
    replacement = repr(value) if isinstance(value, str) else str(value)
    text = valid_job().replace(
        f"{field}: 2" if field == "repetitions" else f"{field}: 1",
        f"{field}: {replacement}",
    )
    with pytest.raises(JobFileError, match=field):
        load_job(write_job(tmp_path, text))


@pytest.mark.parametrize(
    "tasks",
    [
        "  include: []\n  exclude: []",
        "  dataset: d\n  path: p",
        "  dataset: d\n  git: {url: https://e, commit: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa}",
    ],
)
def test_requires_exactly_one_task_source(tmp_path: Path, tasks: str) -> None:
    with pytest.raises(JobFileError, match="exactly one"):
        original = "  dataset: example/data\n  include: [one, two]\n  exclude: []"
        load_job(write_job(tmp_path, valid_job().replace(original, tasks)))


@pytest.mark.parametrize("commit", ["A" * 40, "a" * 39, "g" * 40])
def test_rejects_invalid_arm_commit(tmp_path: Path, commit: str) -> None:
    text = valid_job().replace("a" * 40, commit)
    with pytest.raises(JobFileError, match="commit"):
        load_job(write_job(tmp_path, text))


def test_rejects_invalid_or_duplicate_arm_labels(tmp_path: Path) -> None:
    invalid = valid_job().replace("label: old", "label: Bad")
    with pytest.raises(JobFileError, match="label"):
        load_job(write_job(tmp_path, invalid))
    duplicated = valid_job().replace(
        "arms:\n  -", "arms:\n  - {label: old, harness: opencode, commit: " + "b" * 40 + "}\n  -"
    )
    with pytest.raises(JobFileError, match="unique"):
        load_job(write_job(tmp_path, duplicated))


def test_requires_at_least_one_arm(tmp_path: Path) -> None:
    with pytest.raises(JobFileError, match="at least one arm"):
        load_job(
            write_job(
                tmp_path,
                valid_job().replace(
                    "arms:\n  - {label: old, harness: opencode, commit: " + "a" * 40 + "}",
                    "arms: []",
                ),
            )
        )


def test_rejects_unknown_task_order(tmp_path: Path) -> None:
    with pytest.raises(JobFileError, match="task_order"):
        load_job(write_job(tmp_path, valid_job().replace("as_listed", "random")))


def test_rejects_non_string_task_order(tmp_path: Path) -> None:
    with pytest.raises(JobFileError, match="task_order"):
        load_job(write_job(tmp_path, valid_job().replace("as_listed", "[]")))


def test_resolves_machine_check_relative_to_job_file(tmp_path: Path) -> None:
    job_path = write_job(tmp_path, valid_job())
    assert load_job(job_path).machine_check == (tmp_path / "checks/machine.json").resolve()


def test_fake_arm_keeps_the_opaque_fake_mapping(tmp_path: Path) -> None:
    text = valid_job().replace(
        "  - {label: old, harness: opencode, commit: " + "a" * 40 + "}",
        "  - {label: fake, harness: fake, commit: "
        + "a" * 40
        + ", fake: {requests: 3, solve: false}}",
    )

    job = load_job(write_job(tmp_path, text))

    assert job.arms[0].harness == "fake"
    assert job.arms[0].fake == {"requests": 3, "solve": False}


def test_fake_block_is_opaque_to_the_job_parser(tmp_path: Path) -> None:
    text = valid_job().replace(
        "  - {label: old, harness: opencode, commit: " + "a" * 40 + "}",
        "  - {label: fake, harness: fake, commit: " + "a" * 40 + ", fake: {unknown_behavior: 1}}",
    )

    assert load_job(write_job(tmp_path, text)).arms[0].fake == {"unknown_behavior": 1}


def test_ordinary_arm_has_no_fake_block(tmp_path: Path) -> None:
    assert load_job(write_job(tmp_path, valid_job())).arms[0].fake is None


def test_fake_block_on_a_real_harness_is_rejected(tmp_path: Path) -> None:
    text = valid_job().replace(
        "  - {label: old, harness: opencode, commit: " + "a" * 40 + "}",
        "  - {label: old, harness: opencode, commit: " + "a" * 40 + ", fake: {requests: 3}}",
    )

    with pytest.raises(JobFileError, match="fake"):
        load_job(write_job(tmp_path, text))


def test_fake_block_must_be_a_mapping(tmp_path: Path) -> None:
    text = valid_job().replace(
        "  - {label: old, harness: opencode, commit: " + "a" * 40 + "}",
        "  - {label: fake, harness: fake, commit: " + "a" * 40 + ", fake: [1]}",
    )

    with pytest.raises(JobFileError, match="fake"):
        load_job(write_job(tmp_path, text))


def test_planned_slots_are_task_major(tmp_path: Path) -> None:
    arm = "  - {label: old, harness: opencode, commit: " + "a" * 40 + "}"
    second_arm = "  - {label: new, harness: opencode, commit: " + "b" * 40 + "}"
    job = load_job(
        write_job(tmp_path, valid_job().replace(f"arms:\n{arm}", f"arms:\n{arm}\n{second_arm}"))
    )
    assert planned_slots(job, ["task-a", "task-b"]) == [
        ("task-a", "old"),
        ("task-a", "new"),
        ("task-b", "old"),
        ("task-b", "new"),
    ]


@pytest.mark.parametrize("section", ["tasks", "machine_check"])
def test_requires_job_sections(tmp_path: Path, section: str) -> None:
    text = valid_job()
    if section == "tasks":
        text = text.replace(
            "tasks:\n  dataset: example/data\n  include: [one, two]\n  exclude: []\n", ""
        )
    else:
        text = text.replace("machine_check: checks/machine.json\n", "")
    with pytest.raises(JobFileError, match=section):
        load_job(write_job(tmp_path, text))
