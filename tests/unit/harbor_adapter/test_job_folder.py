import json
import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest

from harbor_research_runner.evidence_bundle.secret_scan import scan
from harbor_research_runner.harbor_adapter.job_folder import (
    HarborFolderError,
    read_trial,
    read_trial_details,
    task_budgets,
    task_digests,
    task_timeouts,
    trial_folders,
)
from harbor_research_runner.trial_validity.phase_table import failed_phase
from harbor_research_runner.trial_validity.verdict import TrialFacts

FIXTURE_ROOT = Path(__file__).parents[2] / "fixtures" / "harbor_job"
TASK_TOMLS = {
    f"terminal-bench/{path.name.removesuffix('.task.toml')}": path.read_text(encoding="utf-8")
    for path in (FIXTURE_ROOT / "task-tomls").glob("*.task.toml")
}
PRODUCT_SETTINGS = {
    "arm_label": "major",
    "harness": "opencode",
    "commit": "0123456789abcdef0123456789abcdef01234567",
    "manifest_path": "harness-manifest.toml",
    "container_host": "host.docker.internal",
    "proxy_port": 8766,
    "variant": None,
    "model_card": {"upstream_model": "model"},
}
PHASE_NAMES = ("environment_setup", "agent_setup", "agent", "verifier")
FIXTURE_FACTS = (
    (
        "pass-bn-fit",
        "bn-fit-modify__xzmPm77",
        "terminal-bench/bn-fit-modify",
        1.0,
        None,
        537197,
        {
            "environment_setup": ("2026-11-11T17:57:50.777553Z", "2026-11-11T17:57:57.904751Z"),
            "agent_setup": ("2026-11-11T17:57:57.904886Z", "2026-11-11T17:58:19.464789Z"),
            "agent": ("2026-11-11T17:58:19.465206Z", "2026-11-11T18:04:11.351112Z"),
            "verifier": ("2026-11-11T18:04:13.953693Z", "2026-11-11T18:05:18.934902Z"),
        },
    ),
    (
        "fail-break-filter",
        "break-filter-js-from-html__U9LxaSP",
        "terminal-bench/break-filter-js-from-html",
        0.0,
        None,
        63645,
        {
            "environment_setup": ("2026-11-11T18:04:38.835377Z", "2026-11-11T18:04:47.546047Z"),
            "agent_setup": ("2026-11-11T18:04:47.546403Z", "2026-11-11T18:05:31.187639Z"),
            "agent": ("2026-11-11T18:05:31.188491Z", "2026-11-11T18:07:06.091277Z"),
            "verifier": ("2026-11-11T18:07:08.567015Z", "2026-11-11T18:07:32.642986Z"),
        },
    ),
    (
        "timeout-git-multibranch-2027run",
        "git-multibranch__oA6a89g",
        "terminal-bench/git-multibranch",
        0.0,
        "AgentTimeoutError",
        491130,
        {
            "environment_setup": ("2026-11-11T21:10:01.468177Z", "2026-11-11T21:10:06.198159Z"),
            "agent_setup": ("2026-11-11T21:10:06.198312Z", "2026-11-11T21:10:30.280008Z"),
            "agent": ("2026-11-11T21:10:30.280538Z", "2026-11-11T21:25:30.827778Z"),
            "verifier": ("2026-11-11T21:25:34.682304Z", "2026-11-11T21:26:12.171869Z"),
        },
    ),
    (
        "nonzeroexit-pytorch-2027run",
        "pytorch-model-cli__esokFHp",
        "terminal-bench/pytorch-model-cli",
        0.0,
        "NonZeroAgentExitCodeError",
        15928,
        {
            "environment_setup": ("2026-11-11T21:09:29.889686Z", "2026-11-11T21:09:32.222610Z"),
            "agent_setup": ("2026-11-11T21:09:32.222805Z", "2026-11-11T21:09:53.715172Z"),
            "agent": ("2026-11-11T21:09:53.715777Z", "2026-11-11T21:10:22.967491Z"),
            "verifier": ("2026-11-11T21:10:26.350544Z", "2026-11-11T21:12:23.747230Z"),
        },
    ),
)


def _epoch(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def _trial_config(task: str, trial: str, *, kwargs: dict | None = None) -> dict:
    return {
        "task": {"name": task, "ref": "sha256:" + "a" * 64, "source": "fixture"},
        "trial_name": trial,
        "agent": {
            "name": "fixture-agent",
            "model_name": "provider/model",
            "kwargs": PRODUCT_SETTINGS.copy() if kwargs is None else kwargs,
        },
    }


def _result(task: str, trial: str, **updates) -> dict:
    result = {
        "task_name": task,
        "trial_name": trial,
        "agent_result": {"n_input_tokens": 2, "n_output_tokens": 3, "n_cache_tokens": 100},
        "verifier_result": {"rewards": {"reward": 1.0}},
        "exception_info": None,
        "environment_setup": {"started_at": "2026-01-01T00:00:00Z", "finished_at": None},
        "agent_setup": None,
        "agent_execution": {"started_at": "2026-01-01T00:00:01Z", "finished_at": None},
        "verifier": None,
        "step_results": [],
    }
    result.update(updates)
    return result


def _write_trial(
    job_dir: Path,
    name: str,
    *,
    task: str = "terminal-bench/test-task",
    config: dict | None = None,
    result: dict | None = None,
    lock: dict | None = None,
) -> Path:
    trial_dir = job_dir / name
    trial_dir.mkdir(parents=True)
    config = _trial_config(task, name) if config is None else config
    (trial_dir / "config.json").write_text(json.dumps(config), encoding="utf-8")
    if result is not None:
        (trial_dir / "result.json").write_text(json.dumps(result), encoding="utf-8")
    if lock is not None:
        (trial_dir / "lock.json").write_text(json.dumps(lock), encoding="utf-8")
    return trial_dir


def _fixture_copy(tmp_path: Path, fixture: str = "pass-bn-fit") -> Path:
    source = FIXTURE_ROOT / fixture
    target = tmp_path / fixture
    shutil.copytree(source, target)
    return target


def test_trial_folders_are_sorted_and_require_config(tmp_path: Path) -> None:
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")
    (tmp_path / "z-trial").mkdir()
    (tmp_path / "a-trial").mkdir()
    (tmp_path / "a-trial" / "config.json").write_text("{}", encoding="utf-8")
    (tmp_path / "z-trial" / "config.json").write_text("{}", encoding="utf-8")
    (tmp_path / "not-a-trial").mkdir()

    assert trial_folders(tmp_path) == [tmp_path / "a-trial", tmp_path / "z-trial"]


def test_trial_folders_of_empty_job_are_empty(tmp_path: Path) -> None:
    assert trial_folders(tmp_path) == []


@pytest.mark.parametrize(
    ("fixture", "trial", "task", "reward", "exception", "tokens", "phase_values"),
    FIXTURE_FACTS,
    ids=[case[0] for case in FIXTURE_FACTS],
)
def test_real_fixture_produces_expected_trial_facts(
    fixture, trial, task, reward, exception, tokens, phase_values
) -> None:
    starts = {phase: _epoch(values[0]) for phase, values in phase_values.items()}
    finishes = {phase: _epoch(values[1]) for phase, values in phase_values.items()}
    expected = TrialFacts(
        trial=trial,
        task=task,
        arm="major",
        has_result=True,
        reward=reward,
        exception_type=exception,
        exception_at=(
            _epoch("2026-11-11T21:25:30.842412Z")
            if fixture == "timeout-git-multibranch-2027run"
            else _epoch("2026-11-11T21:10:22.984025Z")
            if fixture == "nonzeroexit-pytorch-2027run"
            else None
        ),
        phase_started=starts,
        phase_finished=finishes,
        harbor_reported_tokens=tokens,
    )

    facts = read_trial(FIXTURE_ROOT / fixture)
    assert facts == expected
    if fixture in {"timeout-git-multibranch-2027run", "nonzeroexit-pytorch-2027run"}:
        assert failed_phase(facts) == "agent"


def test_trial_without_agent_settings_is_refused_unless_the_caller_names_the_arm(
    tmp_path: Path,
) -> None:
    trial_dir = tmp_path / "oracle-trial"
    shutil.copytree(FIXTURE_ROOT / "pass-bn-fit", trial_dir)
    config = json.loads((trial_dir / "config.json").read_text(encoding="utf-8"))
    config["agent"] = {"name": "oracle"}
    (trial_dir / "config.json").write_text(json.dumps(config), encoding="utf-8")

    with pytest.raises(HarborFolderError, match="missing agent settings"):
        read_trial(trial_dir)
    facts = read_trial(trial_dir, arm="oracle")

    assert facts.task == "terminal-bench/bn-fit-modify"
    assert facts.arm == "oracle"


def test_local_task_config_uses_path_name_when_task_name_is_absent(tmp_path: Path) -> None:
    trial_dir = tmp_path / "local-trial"
    shutil.copytree(FIXTURE_ROOT / "pass-bn-fit", trial_dir)
    task_dir = tmp_path / "say-hello"
    task_dir.mkdir()
    (task_dir / "task.toml").write_text('[task]\nname = "canary/say-hello"\n', encoding="utf-8")
    config = json.loads((trial_dir / "config.json").read_text(encoding="utf-8"))
    config["task"] = {"path": str(task_dir)}
    config["agent"] = {"name": "oracle"}
    (trial_dir / "config.json").write_text(json.dumps(config), encoding="utf-8")
    result = json.loads((trial_dir / "result.json").read_text(encoding="utf-8"))
    result["task_name"] = "canary/say-hello"
    (trial_dir / "result.json").write_text(json.dumps(result), encoding="utf-8")

    facts = read_trial(trial_dir, arm="oracle")

    assert facts.task == "canary/say-hello"
    assert facts.arm == "oracle"


def test_local_task_without_a_task_table_is_named_by_its_folder_as_harbor_does(
    tmp_path: Path,
) -> None:
    trial_dir = tmp_path / "local-trial"
    shutil.copytree(FIXTURE_ROOT / "pass-bn-fit", trial_dir)
    task_dir = tmp_path / "say-hello"
    task_dir.mkdir()
    (task_dir / "task.toml").write_text("[agent]\ntimeout_sec = 60\n", encoding="utf-8")
    config = json.loads((trial_dir / "config.json").read_text(encoding="utf-8"))
    config["task"] = {"path": str(task_dir)}
    (trial_dir / "config.json").write_text(json.dumps(config), encoding="utf-8")
    result = json.loads((trial_dir / "result.json").read_text(encoding="utf-8"))
    result["task_name"] = "say-hello"
    (trial_dir / "result.json").write_text(json.dumps(result), encoding="utf-8")

    assert read_trial(trial_dir, arm="oracle").task == "say-hello"


def test_trial_reader_accepts_the_machine_check_agent_label(tmp_path: Path) -> None:
    trial_dir = tmp_path / "local-trial"
    shutil.copytree(FIXTURE_ROOT / "pass-bn-fit", trial_dir)
    task_dir = tmp_path / "say-hello"
    task_dir.mkdir()
    (task_dir / "task.toml").write_text('[task]\nname = "say-hello"\n', encoding="utf-8")
    config = json.loads((trial_dir / "config.json").read_text(encoding="utf-8"))
    config["task"] = {"path": str(task_dir)}
    config.pop("agent")
    (trial_dir / "config.json").write_text(json.dumps(config), encoding="utf-8")
    result = json.loads((trial_dir / "result.json").read_text(encoding="utf-8"))
    result["task_name"] = "say-hello"
    (trial_dir / "result.json").write_text(json.dumps(result), encoding="utf-8")

    facts = read_trial(trial_dir, arm="oracle")

    assert facts.task == "say-hello"
    assert facts.arm == "oracle"


@pytest.mark.parametrize("result_text", [None, "", "not-json", "[]"])
def test_missing_empty_unparseable_or_non_object_result_has_no_result(
    tmp_path: Path, result_text: str | None
) -> None:
    trial = _write_trial(
        tmp_path, "trial__abc", result=_result("terminal-bench/test-task", "trial__abc")
    )
    if result_text is None:
        (trial / "result.json").unlink()
    else:
        (trial / "result.json").write_text(result_text, encoding="utf-8")

    assert read_trial(trial).has_result is False


@pytest.mark.parametrize("missing_key", ["agent_execution", "exception_info"])
def test_result_missing_required_phase_or_exception_key_has_no_result(
    tmp_path: Path, missing_key: str
) -> None:
    task = "terminal-bench/test-task"
    result = _result(task, "trial__abc")
    del result[missing_key]
    trial = _write_trial(tmp_path, "trial__abc", result=result)

    assert read_trial(trial).has_result is False


def test_task_is_the_full_name_not_the_truncated_trial_folder(tmp_path: Path) -> None:
    task = "terminal-bench/" + "x" * 40
    trial_name = "x" * 32 + "__abc1234"
    trial = _write_trial(
        tmp_path,
        trial_name,
        task=task,
        result=_result(task, trial_name),
    )

    assert read_trial(trial).task == task


def test_unreadable_trial_config_raises_with_trial_and_file(tmp_path: Path) -> None:
    trial = tmp_path / "trial__bad"
    trial.mkdir()
    (trial / "config.json").write_text("not-json", encoding="utf-8")

    with pytest.raises(HarborFolderError, match=r"trial__bad.*config\.json"):
        read_trial(trial)


def test_old_agent_kwargs_without_arm_label_are_rejected(tmp_path: Path) -> None:
    trial = _fixture_copy(tmp_path)
    config_path = trial / "config.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["agent"]["kwargs"] = {"provider": "provider", "variant": "xhigh"}
    config_path.write_text(json.dumps(config), encoding="utf-8")

    with pytest.raises(HarborFolderError, match=r"arm_label"):
        read_trial(trial)


def test_trial_task_name_mismatch_is_rejected(tmp_path: Path) -> None:
    trial = _write_trial(
        tmp_path,
        "trial__mismatch",
        task="terminal-bench/config-task",
        result=_result("terminal-bench/result-task", "trial__mismatch"),
    )

    with pytest.raises(HarborFolderError, match=r"task.*result\.json"):
        read_trial(trial)


def test_nonempty_step_results_are_rejected(tmp_path: Path) -> None:
    task = "terminal-bench/test-task"
    result = _result(task, "trial__steps", step_results=[{"reward": 1.0}])
    trial = _write_trial(tmp_path, "trial__steps", result=result)

    with pytest.raises(HarborFolderError, match=r"step_results"):
        read_trial(trial)


@pytest.mark.parametrize(
    ("rewards", "expected"),
    [
        ({"reward": 1.0, "other": 0.0}, 1.0),
        ({"other": 0.25}, 0.25),
        ({"one": 0.25, "two": 0.75}, None),
        ({"reward": True}, None),
        ({"reward": "0.5"}, 0.5),
    ],
)
def test_reward_selection_matches_old_cases(
    tmp_path: Path, rewards: dict, expected: float | None
) -> None:
    task = "terminal-bench/test-task"
    trial = _write_trial(
        tmp_path,
        "trial__reward",
        result=_result(task, "trial__reward", verifier_result={"rewards": rewards}),
    )

    assert read_trial(trial).reward == expected


def test_harbor_reported_tokens_sum_input_and_output_without_adding_cache() -> None:
    assert read_trial(FIXTURE_ROOT / "pass-bn-fit").harbor_reported_tokens == 537197


def test_agent_wall_seconds_come_from_agent_execution_bounds() -> None:
    details = read_trial_details(FIXTURE_ROOT / "pass-bn-fit")

    assert details.agent_wall_s == pytest.approx(351.885906)


def test_agent_wall_seconds_are_none_without_both_bounds(tmp_path: Path) -> None:
    task = "terminal-bench/test-task"
    trial = _write_trial(
        tmp_path,
        "trial__wall",
        result=_result(task, "trial__wall", agent_execution={"started_at": None}),
    )

    assert read_trial_details(trial).agent_wall_s is None


def test_naive_phase_timestamps_are_utc(tmp_path: Path) -> None:
    task = "terminal-bench/test-task"
    result = _result(
        task,
        "trial__naive",
        environment_setup={
            "started_at": "2026-01-01T00:00:00",
            "finished_at": "2026-01-01T00:00:01+00:00",
        },
    )
    trial = _write_trial(tmp_path, "trial__naive", result=result)

    assert (
        read_trial(trial).phase_started["environment_setup"]
        == datetime(2026, 1, 1, tzinfo=UTC).timestamp()
    )


def test_turns_and_tool_calls_from_trimmed_pass_trajectory() -> None:
    details = read_trial_details(FIXTURE_ROOT / "pass-bn-fit")

    assert details.turns == 26
    assert details.tool_calls == 25


def test_turns_fall_back_to_trajectory_step_count(tmp_path: Path) -> None:
    task = "terminal-bench/test-task"
    trial = _write_trial(tmp_path, "trial__trajectory", result=_result(task, "trial__trajectory"))
    (trial / "agent").mkdir()
    (trial / "agent" / "trajectory.json").write_text(
        json.dumps({"steps": [{}, {}, {}]}), encoding="utf-8"
    )

    assert read_trial_details(trial).turns == 3


def test_turns_use_final_metrics_without_a_step_list(tmp_path: Path) -> None:
    task = "terminal-bench/test-task"
    trial = _write_trial(tmp_path, "trial__metrics", result=_result(task, "trial__metrics"))
    (trial / "agent").mkdir()
    trajectory = {"final_metrics": {"total_steps": 7}}
    (trial / "agent" / "trajectory.json").write_text(json.dumps(trajectory), encoding="utf-8")

    details = read_trial_details(trial)
    assert details.turns == 7
    assert details.tool_calls is None


def test_tool_calls_count_every_function_name(tmp_path: Path) -> None:
    task = "terminal-bench/test-task"
    trial = _write_trial(tmp_path, "trial__tools", result=_result(task, "trial__tools"))
    (trial / "agent").mkdir()
    trajectory = {
        "steps": [
            {"tool_calls": [{"function_name": "bash"}, {"function_name": "read"}]},
            {"tool_calls": [{"function_name": "bash"}, {"function_name": None}]},
        ]
    }
    (trial / "agent" / "trajectory.json").write_text(json.dumps(trajectory), encoding="utf-8")

    assert read_trial_details(trial).tool_calls == 3


@pytest.mark.parametrize(
    ("trajectory_text", "expected_turns"),
    [(None, None), ("not-json", None), (json.dumps({"steps": []}), 0)],
)
def test_missing_unparseable_or_empty_trajectory_has_expected_counts(
    tmp_path: Path, trajectory_text: str | None, expected_turns: int | None
) -> None:
    task = "terminal-bench/test-task"
    trial = _write_trial(
        tmp_path, "trial__no-trajectory", result=_result(task, "trial__no-trajectory")
    )
    if trajectory_text is not None:
        (trial / "agent").mkdir()
        (trial / "agent" / "trajectory.json").write_text(trajectory_text, encoding="utf-8")

    details = read_trial_details(trial)
    assert details.turns == expected_turns
    assert details.tool_calls is None


@pytest.mark.parametrize(
    ("fixture", "expected"),
    [
        ("pass-bn-fit", False),
        ("timeout-git-multibranch-2027run", False),
        ("nonzeroexit-pytorch-2027run", True),
    ],
)
def test_agent_exit_nonzero_uses_exception_and_failed_phase(fixture: str, expected: bool) -> None:
    assert read_trial_details(FIXTURE_ROOT / fixture).agent_exit_nonzero is expected


def test_verifier_exception_is_not_agent_exit_nonzero(tmp_path: Path) -> None:
    task = "terminal-bench/test-task"
    result = _result(
        task,
        "trial__verifier-error",
        exception_info={"exception_type": "VerifierTimeoutError"},
        verifier={"started_at": "2026-01-01T00:00:02Z", "finished_at": None},
    )
    trial = _write_trial(tmp_path, "trial__verifier-error", result=result)

    assert read_trial_details(trial).agent_exit_nonzero is False


def test_non_timeout_exception_is_agent_exit_when_agent_is_last_started(tmp_path: Path) -> None:
    task = "terminal-bench/test-task"
    result = _result(
        task,
        "trial__agent-error",
        exception_info={"exception_type": "NonZeroAgentExitCodeError"},
        verifier=None,
    )
    trial = _write_trial(tmp_path, "trial__agent-error", result=result)

    assert read_trial_details(trial).agent_exit_nonzero is True


def test_harness_commit_is_read_from_product_settings() -> None:
    assert (
        read_trial_details(FIXTURE_ROOT / "pass-bn-fit").harness_commit
        == PRODUCT_SETTINGS["commit"]
    )


def test_null_harness_commit_is_preserved(tmp_path: Path) -> None:
    task = "terminal-bench/test-task"
    kwargs = PRODUCT_SETTINGS | {"commit": None}
    trial = _write_trial(
        tmp_path,
        "trial__null-commit",
        config=_trial_config(task, "trial__null-commit", kwargs=kwargs),
        result=_result(task, "trial__null-commit"),
    )

    assert read_trial_details(trial).harness_commit is None


def test_missing_harness_commit_setting_is_rejected(tmp_path: Path) -> None:
    task = "terminal-bench/test-task"
    kwargs = PRODUCT_SETTINGS.copy()
    del kwargs["commit"]
    trial = _write_trial(
        tmp_path,
        "trial__missing-commit",
        config=_trial_config(task, "trial__missing-commit", kwargs=kwargs),
        result=_result(task, "trial__missing-commit"),
    )

    with pytest.raises(HarborFolderError, match=r"commit"):
        read_trial_details(trial)


def test_task_budgets_use_supplied_task_tomls_and_trial_locks() -> None:
    assert task_budgets(FIXTURE_ROOT, TASK_TOMLS) == {
        "terminal-bench/bn-fit-modify": (3600.0, 3600.0),
        "terminal-bench/break-filter-js-from-html": (1200.0, 1200.0),
        "terminal-bench/git-multibranch": (900.0, 900.0),
        "terminal-bench/pytorch-model-cli": (900.0, 900.0),
    }


def test_task_timeouts_reads_agent_and_verifier_budgets() -> None:
    assert task_timeouts(
        "terminal-bench/test-task",
        "[agent]\ntimeout_sec = 600\n[verifier]\ntimeout_sec = 45\n",
    ) == (600.0, 45.0)


def test_task_timeouts_reports_the_task_when_agent_timeout_is_missing() -> None:
    with pytest.raises(HarborFolderError, match="terminal-bench/test-task"):
        task_timeouts(
            "terminal-bench/test-task",
            "[verifier]\ntimeout_sec = 45\n",
        )


def test_task_timeouts_reports_the_task_when_toml_is_invalid() -> None:
    with pytest.raises(HarborFolderError, match="terminal-bench/test-task"):
        task_timeouts("terminal-bench/test-task", "[agent\ntimeout_sec = 600\n")


def test_task_budget_uses_override_and_lock_multiplier(tmp_path: Path) -> None:
    task = "terminal-bench/bn-fit-modify"
    config = _trial_config(task, "trial__budget")
    config["agent"]["override_timeout_sec"] = 300
    lock = {"task": {"name": task}, "timeout_multiplier": 2.0}
    _write_trial(tmp_path, "trial__budget", task=task, config=config, lock=lock)

    assert task_budgets(tmp_path, {task: TASK_TOMLS[task]}) == {task: (600.0, 7200.0)}


def test_task_budget_without_supplied_task_toml_is_rejected(tmp_path: Path) -> None:
    task = "terminal-bench/test-task"
    _write_trial(tmp_path, "trial__missing-budget", task=task, lock={"task": {"name": task}})

    with pytest.raises(HarborFolderError, match=r"test-task"):
        task_budgets(tmp_path, {})


@pytest.mark.parametrize("task_toml", ["[agent]\ntimeout_sec=1\n", "[verifier]\ntimeout_sec=1\n"])
def test_task_toml_missing_either_timeout_is_rejected(tmp_path: Path, task_toml: str) -> None:
    task = "terminal-bench/test-task"
    _write_trial(tmp_path, "trial__bad-toml", task=task, lock={"task": {"name": task}})

    with pytest.raises(HarborFolderError, match=r"timeout_sec"):
        task_budgets(tmp_path, {task: task_toml})


def test_task_budget_refuses_conflicting_trials(tmp_path: Path) -> None:
    task = "terminal-bench/bn-fit-modify"
    _write_trial(
        tmp_path,
        "trial__one",
        task=task,
        lock={"task": {"name": task}, "timeout_multiplier": 1.0},
    )
    _write_trial(
        tmp_path,
        "trial__two",
        task=task,
        lock={"task": {"name": task}, "timeout_multiplier": 2.0},
    )

    with pytest.raises(HarborFolderError, match=r"conflicting.*budget"):
        task_budgets(tmp_path, {task: TASK_TOMLS[task]})


def test_task_digests_read_trial_locks() -> None:
    assert task_digests(FIXTURE_ROOT) == {
        "terminal-bench/bn-fit-modify": "sha256:b5f9644970c17ad9ddb46b7266f7bcd87c761d77d7e6f55d7cfe7284d5ff66e9",
        "terminal-bench/break-filter-js-from-html": "sha256:678008d1a4fd1e6e1b9b3cc9a327219fe4b410a31eafc52e9099bbf947eea600",
        "terminal-bench/git-multibranch": "sha256:7759a92a373c967dd31535975bad8659410644025673fe62385444fd2605c5fa",
        "terminal-bench/pytorch-model-cli": "sha256:6f11544dcc81a380dc4611902961f5520259c462d9fceb413419d66ab9a96c79",
    }


def test_task_digests_refuse_conflicting_trial_locks(tmp_path: Path) -> None:
    task = "terminal-bench/test-task"
    for name, digest in (
        ("trial__one", "sha256:" + "a" * 64),
        ("trial__two", "sha256:" + "b" * 64),
    ):
        _write_trial(
            tmp_path,
            name,
            task=task,
            lock={"task": {"name": task, "digest": digest}},
        )

    with pytest.raises(HarborFolderError, match=r"conflicting.*digest"):
        task_digests(tmp_path)


def test_harbor_fixture_secret_scan_has_no_findings() -> None:
    files = {
        path.relative_to(FIXTURE_ROOT).as_posix(): path.read_bytes()
        for path in FIXTURE_ROOT.rglob("*")
        if path.is_file()
    }

    assert scan(files, ()) == []


def test_fixture_agent_kwargs_use_the_product_settings_shape() -> None:
    expected_keys = {
        "arm_label",
        "harness",
        "commit",
        "manifest_path",
        "container_host",
        "proxy_port",
        "variant",
        "model_card",
    }
    for fixture, *_ in FIXTURE_FACTS:
        config = json.loads((FIXTURE_ROOT / fixture / "config.json").read_text(encoding="utf-8"))
        assert set(config["agent"]["kwargs"]) == expected_keys
        assert "api_key" not in config["agent"]["kwargs"]
    job_config = json.loads((FIXTURE_ROOT / "config.json").read_text(encoding="utf-8"))
    assert set(job_config["agents"][0]["kwargs"]) == expected_keys
    assert "api_key" not in job_config["agents"][0]["kwargs"]
