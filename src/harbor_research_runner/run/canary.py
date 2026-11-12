from dataclasses import replace
from pathlib import Path

from harbor_research_runner.harbor_adapter.commands import job_config
from harbor_research_runner.job_config.job_file import Job, TaskSource
from harbor_research_runner.job_config.model_cards import ModelCard
from harbor_research_runner.trial_validity.verdict import TrialFacts, Verdict

CANARY_TASK = Path(__file__).parent / "canary_task" / "say-hello"


def canary_job_config(
    job: Job,
    agents: list[tuple[str, dict]],
    verifier_timeout_multiplier: float,
) -> dict:
    canary_job = replace(
        job,
        repetitions=1,
        tasks=TaskSource(
            kind="path",
            dataset=None,
            path=str(CANARY_TASK.parent),
            git_url=None,
            git_commit=None,
            include=(),
            exclude=(),
        ),
    )
    return job_config(
        canary_job,
        [CANARY_TASK.name],
        agents,
        verifier_timeout_multiplier,
        job_name="canary-job",
    )


def canary_passed(
    trial_facts: list[TrialFacts],
    verdicts: list[Verdict],
    ledger_rows: list[dict],
    card: ModelCard,
    arms: list[str],
) -> tuple[bool, str]:
    for arm in arms:
        arm_trials = [(index, facts) for index, facts in enumerate(trial_facts) if facts.arm == arm]
        if len(arm_trials) != 1:
            return False, f"arm {arm} has {len(arm_trials)} canary trials; expected exactly one"

        index, facts = arm_trials[0]
        if index >= len(verdicts):
            return False, f"arm {arm} is missing a canary verdict"
        if not verdicts[index].valid:
            return False, f"arm {arm} canary trial is invalid"
        if facts.reward != 1.0:
            return False, f"arm {arm} canary reward is {facts.reward!r}; expected 1.0"

        if not any(
            row.get("type") == "request_end"
            and row.get("trial") == facts.trial
            and row.get("upstream_status") == "ok"
            and row.get("served_model") == card.upstream_model
            for row in ledger_rows
        ):
            return False, f"arm {arm} has no successful request served by the pinned served_model"

    return True, "canary passed"
