from __future__ import annotations

import argparse
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from harbor_research_runner import cli
from harbor_research_runner.docker_host import cleanup
from harbor_research_runner.job_config.job_file import Job
from harbor_research_runner.run.loop import RunOutcome


def test_cli_path_arguments_are_absolute_with_relative_values_and_defaults() -> None:
    parser = cli._parser()
    commands = next(
        action for action in parser._actions if isinstance(action, argparse._SubParsersAction)
    )
    relative_argv = {
        "build-harness": ["--harness", "opencode", "--commit", "abc"],
        "check-machine": ["--job", "job.yaml", "--out", "check.json"],
        "run": ["study.yaml", "--no-evidence"],
        "build-evidence": ["run-1"],
        "explore": [],
        "clean-docker": [],
    }
    for command_name, command_parser in commands.choices.items():
        assert not [action.dest for action in command_parser._actions if action.type is Path]
        path_actions = [
            action for action in command_parser._actions if action.type is cli._absolute_path
        ]
        assert path_actions
        for action in path_actions:
            argv = [command_name, *relative_argv[command_name]]
            value = "relative-path"
            if action.option_strings:
                argv.extend([action.option_strings[0], value])
            elif command_name == "run":
                argv[1] = value
            else:
                argv.append(value)
            parsed = parser.parse_args(argv)
            assert all(
                path.is_absolute()
                for path_action in path_actions
                for path in _as_path_list(getattr(parsed, path_action.dest))
            )
        parsed_defaults = parser.parse_args([command_name, *relative_argv[command_name]])
        assert all(
            path.is_absolute()
            for action in path_actions
            for path in _as_path_list(getattr(parsed_defaults, action.dest))
        )


def _as_path_list(value: Path | list[Path]) -> list[Path]:
    return value if isinstance(value, list) else [value]


def test_build_harness_parses_options_and_calls_builder(tmp_path: Path) -> None:
    calls = []

    def build(harness, commit, manifest_path):
        calls.append((harness, commit, manifest_path))
        return 0

    manifest = tmp_path / "manifest.toml"
    assert (
        cli.main(
            [
                "build-harness",
                "--harness",
                "opencode",
                "--commit",
                "abc",
                "--manifest",
                str(manifest),
            ],
            steps={"build_harness": build},
        )
        == 0
    )
    assert calls == [("opencode", "abc", manifest)]


def test_check_machine_parses_options_and_calls_checker(tmp_path: Path) -> None:
    calls = []

    def check(job, out, with_do_nothing, runs_dir, deps):
        assert isinstance(job, Job)
        calls.append((job, out, with_do_nothing, runs_dir, deps))
        return SimpleNamespace(included=[])

    job = tmp_path / "job.yaml"
    job.write_text(
        "name: study\n"
        "model: provider/model\n"
        "repetitions: 1\n"
        "concurrency: 1\n"
        "task_order: as_listed\n"
        "tasks: {dataset: bench/tasks}\n"
        "machine_check: check.json\n"
        "arms: [{label: arm, harness: fake, commit: " + "a" * 40 + ", fake: {}}]\n"
        "proxy: {host: 127.0.0.1, port: 8766, container_host: host.docker.internal}\n"
        "limits: {}\n",
        encoding="utf-8",
    )
    out = tmp_path / "check.json"
    runs = tmp_path / "runs"
    marker = SimpleNamespace(host_lock=SimpleNamespace(release=lambda: None))
    assert (
        cli.main(
            [
                "check-machine",
                "--job",
                str(job),
                "--out",
                str(out),
                "--with-do-nothing",
                "--runs-dir",
                str(runs),
            ],
            steps={"check_machine": check, "check_deps": lambda: marker},
        )
        == 0
    )
    assert isinstance(calls[0][0], Job)
    assert calls[0][1:4] == (out, True, runs)
    assert calls[0][4].checks is marker


def test_run_builds_evidence_for_complete_outcome(tmp_path: Path) -> None:
    run_dir = tmp_path / "runs" / "r1"
    calls = []

    def run_job(job, runs, manifest, environment, deps):
        calls.append(("run", job, runs, manifest, environment, deps))
        return RunOutcome(0, run_dir)

    def build_evidence(actual_run_dir, evidence_dir, environment):
        calls.append(("evidence", actual_run_dir, evidence_dir, environment))
        return 0

    job = tmp_path / "job.yaml"
    runs = tmp_path / "runs"
    evidence = tmp_path / "evidence"
    manifest = tmp_path / "manifest.toml"
    assert (
        cli.main(
            [
                "run",
                str(job),
                "--runs-dir",
                str(runs),
                "--evidence-dir",
                str(evidence),
                "--manifest",
                str(manifest),
            ],
            steps={
                "run_job": run_job,
                "run_deps": lambda: "deps",
                "build_evidence": build_evidence,
            },
        )
        == 0
    )
    assert calls[0][:4] == ("run", job, runs, manifest)
    assert calls[0][4] == os.environ.copy()
    assert calls[0][5] == "deps"
    assert calls[1][:3] == ("evidence", run_dir, evidence / "r1")
    assert calls[1][3] == calls[0][4]


def test_run_does_not_build_evidence_for_halted_outcome(tmp_path: Path) -> None:
    calls = []
    assert (
        cli.main(
            ["run", str(tmp_path / "job.yaml")],
            steps={
                "run_job": lambda *_args: RunOutcome(2, tmp_path / "runs" / "r1"),
                "run_deps": lambda: object(),
                "build_evidence": lambda *_args: calls.append(True),
            },
        )
        == 2
    )
    assert calls == []


def test_run_resume_routes_run_id_to_resume_function(tmp_path: Path) -> None:
    calls = []
    assert (
        cli.main(
            ["run", "--resume", "run-1", "--no-evidence"],
            steps={
                "resume_run": lambda run_id, runs, manifest, environment, deps: (
                    calls.append((run_id, runs, manifest, deps)) or RunOutcome(2, None)
                ),
                "run_deps": lambda: "deps",
            },
        )
        == 2
    )
    assert calls == [
        ("run-1", Path("runs").resolve(), Path("harness-manifest.toml").resolve(), "deps")
    ]


def test_run_no_evidence_skips_bundle_build(tmp_path: Path) -> None:
    calls = []
    assert (
        cli.main(
            ["run", str(tmp_path / "job.yaml"), "--no-evidence"],
            steps={
                "run_job": lambda *_args: RunOutcome(3, tmp_path / "runs" / "r1"),
                "run_deps": lambda: object(),
                "build_evidence": lambda *_args: calls.append(True),
            },
        )
        == 3
    )
    assert calls == []


def test_run_secret_hit_returns_four(tmp_path: Path) -> None:
    assert (
        cli.main(
            ["run", str(tmp_path / "job.yaml")],
            steps={
                "run_job": lambda *_args: RunOutcome(0, tmp_path / "runs" / "r1"),
                "run_deps": lambda: object(),
                "build_evidence": lambda *_args: 4,
            },
        )
        == 4
    )


def test_build_evidence_uses_run_id_and_directories(tmp_path: Path) -> None:
    calls = []
    runs = tmp_path / "runs"
    evidence = tmp_path / "evidence"
    assert (
        cli.main(
            ["build-evidence", "r1", "--runs-dir", str(runs), "--evidence-dir", str(evidence)],
            steps={"build_evidence": lambda *args: calls.append(args) or 0},
        )
        == 0
    )
    assert calls[0][:2] == (runs / "r1", evidence / "r1")
    assert calls[0][2] == os.environ.copy()


def test_explore_serves_the_evidence_paths_on_requested_port(tmp_path: Path) -> None:
    calls = []

    class Server:
        def serve_forever(self):
            calls.append("serve")

        def server_close(self):
            calls.append("close")

    paths = [tmp_path / "one", tmp_path / "two"]
    for path in paths:
        path.mkdir()
        (path / "manifest.json").touch()
        (path / "trials.jsonl").touch()
    assert (
        cli.main(
            ["explore", *map(str, paths), "--port", "9000"],
            steps={
                "build_server": lambda store, port: calls.append((store.paths, port)) or Server()
            },
        )
        == 0
    )
    assert calls == [(paths, 9000), "serve", "close"]


def test_unknown_subcommand_exits_two_with_usage(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as error:
        cli.main(["not-a-command"])
    assert error.value.code == 2
    assert "usage:" in capsys.readouterr().err


def test_version_prints_package_metadata(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as error:
        cli.main(["--version"])
    assert error.value.code == 0
    assert "0.1.0" in capsys.readouterr().out


def test_clean_docker_dry_run_is_default(tmp_path: Path) -> None:
    calls = []
    assert (
        cli.main(
            ["clean-docker", "--runs-dir", str(tmp_path / "runs")],
            steps={
                "clean_docker": lambda apply, lock, runs_dir: (
                    calls.append((apply, runs_dir))
                    or SimpleNamespace(
                        planned_containers=(), planned_networks=(), skipped_attached=()
                    )
                )
            },
        )
        == 0
    )
    assert calls == [(False, tmp_path / "runs")]


def test_clean_docker_apply_passes_flag(tmp_path: Path) -> None:
    calls = []
    assert (
        cli.main(
            ["clean-docker", "--apply"],
            steps={
                "clean_docker": lambda apply, lock, runs_dir: (
                    calls.append(apply)
                    or SimpleNamespace(
                        planned_containers=("container",),
                        planned_networks=(),
                        removed_containers=("container",),
                        removed_networks=(),
                        skipped_attached=(),
                    )
                )
            },
        )
        == 0
    )
    assert calls == [True]


def test_clean_docker_logs_planned_skipped_and_removed_names(
    caplog: pytest.LogCaptureFixture,
) -> None:
    report = SimpleNamespace(
        planned_containers=("container-a",),
        planned_networks=("network-a",),
        removed_containers=("container-a",),
        removed_networks=("network-a",),
        skipped_attached=("network-b",),
    )

    with caplog.at_level("INFO", logger="harbor_research_runner.cli"):
        assert (
            cli.main(
                ["clean-docker", "--apply"],
                steps={"clean_docker": lambda *_args, **_kwargs: report},
            )
            == 0
        )

    assert "container-a" in caplog.text
    assert "network-a" in caplog.text
    assert "network-b" in caplog.text
    assert "removed" in caplog.text


def test_clean_docker_daemon_failure_returns_one() -> None:
    assert (
        cli.main(
            ["clean-docker"],
            steps={
                "clean_docker": lambda *_args, **_kwargs: (_ for _ in ()).throw(
                    RuntimeError("daemon down")
                )
            },
        )
        == 1
    )


def test_clean_docker_active_run_returns_one() -> None:
    assert (
        cli.main(
            ["clean-docker", "--apply"],
            steps={
                "clean_docker": lambda *_args, **_kwargs: (_ for _ in ()).throw(
                    cleanup.RunActive("active")
                )
            },
        )
        == 1
    )


def test_cli_does_not_read_environment_variables_by_name() -> None:
    source = Path(cli.__file__).read_text(encoding="utf-8")
    assert "os.environ[" not in source
    assert "os.getenv" not in source
    assert "environ.get" not in source


@pytest.mark.parametrize("run_id", ["../other-run", "nested/run", "/abs/run", "..", "."])
@pytest.mark.parametrize("command", [["run", "--resume"], ["build-evidence"]])
def test_run_id_that_is_not_one_folder_name_exits_two(
    command: list[str], run_id: str, capsys: pytest.CaptureFixture[str]
) -> None:
    called = []
    steps = {
        name: lambda *args, **kwargs: called.append(args)
        for name in ("resume_run", "build_evidence")
    }

    with pytest.raises(SystemExit) as exit_info:
        cli.main([*command, run_id], steps=steps)

    assert exit_info.value.code == 2
    assert called == []
    assert "run id must be one folder name" in capsys.readouterr().err
