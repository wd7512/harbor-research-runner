from pathlib import Path

from harbor.models.job.config import DatasetConfig
from harbor.models.task.id import LocalTaskId

from harbor_research_runner.job_config.job_file import Arm, Job, TaskSource
from harbor_research_runner.job_config.model_cards import ModelCard
from harbor_research_runner.run.canary import canary_job_config, canary_passed
from harbor_research_runner.trial_validity.verdict import TrialFacts, Verdict


def _facts(arm: str, *, reward: float | None = 1.0) -> TrialFacts:
    return TrialFacts(
        trial=f"say-hello__{arm}",
        task="canary/say-hello",
        arm=arm,
        has_result=True,
        reward=reward,
        exception_type=None,
        exception_at=None,
        phase_started={},
        phase_finished={},
        harbor_reported_tokens=1,
    )


def _verdict(valid: bool = True) -> Verdict:
    return Verdict(valid, None if valid else "never_finished", None, (), True, None)


def _end(trial: str, *, status: str = "ok", served_model: str = "pinned-model") -> dict:
    return {
        "type": "request_end",
        "trial": trial,
        "upstream_status": status,
        "served_model": served_model,
    }


def _card() -> ModelCard:
    return ModelCard(
        id="provider/model",
        upstream_model="pinned-model",
        api="chat",
        endpoints=[],
        key_env=[],
        tokens_per_minute=None,
        pricing={"input": 0.0, "output": 0.0, "cached_read": 0.0},
        variants={},
        withdrawn=False,
        reason=None,
    )


def _job() -> Job:
    return Job(
        path=Path("job.yaml"),
        name="study",
        model="provider/model",
        repetitions=3,
        concurrency=2,
        task_order="as_listed",
        tasks=TaskSource("dataset", "dataset/name", None, None, None, (), ()),
        machine_check=Path("check.json"),
        arms=(Arm("arm-a", "fake", "a" * 40),),
        proxy_host="127.0.0.1",
        proxy_port=8766,
        container_host="host.docker.internal",
        limit_overrides={},
    )


def test_canary_job_config_uses_the_bundled_task_once() -> None:
    config = canary_job_config(_job(), [("agent.path:Agent", {"arm_label": "arm-a"})], 1.5)

    assert config["job_name"] == "canary-job"
    assert config["n_attempts"] == 1
    assert config["datasets"] == [
        {
            "path": str(Path(__file__).parents[3] / "src/harbor_research_runner/run/canary_task"),
            "task_names": ["say-hello"],
        }
    ]
    assert config["agents"] == [
        {"import_path": "agent.path:Agent", "kwargs": {"arm_label": "arm-a"}}
    ]


def test_harbor_selects_the_canary_task_from_its_dataset() -> None:
    dataset = canary_job_config(_job(), [("agent.path:Agent", {"arm_label": "arm-a"})], 1.5)[
        "datasets"
    ][0]
    source = Path(dataset["path"])
    task_ids = [LocalTaskId(path=path) for path in source.iterdir() if path.is_dir()]

    selected = DatasetConfig.model_validate(dataset)._filter_task_ids(task_ids)

    assert [task_id.get_name() for task_id in selected] == ["say-hello"]


def test_canary_passes_for_every_arm() -> None:
    facts = [_facts("arm-a"), _facts("arm-b")]
    verdicts = [_verdict(), _verdict()]
    rows = [_end(fact.trial) for fact in facts]

    passed, message = canary_passed(facts, verdicts, rows, _card(), ["arm-a", "arm-b"])

    assert passed is True
    assert message == "canary passed"


def test_canary_fails_when_an_arm_is_missing() -> None:
    passed, message = canary_passed(
        [_facts("arm-a")], [_verdict()], [_end(_facts("arm-a").trial)], _card(), ["arm-a", "arm-b"]
    )

    assert passed is False
    assert "arm-b" in message


def test_canary_fails_when_reward_is_zero() -> None:
    fact = _facts("arm-a", reward=0.0)

    passed, message = canary_passed([fact], [_verdict()], [_end(fact.trial)], _card(), ["arm-a"])

    assert passed is False
    assert "arm-a" in message
    assert "reward" in message


def test_canary_fails_when_trial_is_invalid() -> None:
    fact = _facts("arm-a")

    passed, message = canary_passed(
        [fact], [_verdict(False)], [_end(fact.trial)], _card(), ["arm-a"]
    )

    assert passed is False
    assert "arm-a" in message
    assert "valid" in message


def test_served_model_mismatch_fails_canary() -> None:
    fact = _facts("arm-a")

    passed, message = canary_passed(
        [fact], [_verdict()], [_end(fact.trial, served_model="other-model")], _card(), ["arm-a"]
    )

    assert passed is False
    assert "arm-a" in message
    assert "served_model" in message
