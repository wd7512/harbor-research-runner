import pytest

from harbor_research_runner.records.trials import TrialRow


def _trial_row():
    return TrialRow(
        trial="trial",
        task="task",
        arm="arm",
        harness="opencode",
        commit="c" * 40,
        model="provider/model",
        location="harbor-job",
        is_first_attempt=True,
        is_final=True,
        reward=1.0,
        is_pass=True,
        harbor_exception=None,
        agent_exit_nonzero=False,
        environment_setup_started_t=1.0,
        environment_setup_finished_t=2.0,
        agent_setup_started_t=2.0,
        agent_setup_finished_t=3.0,
        agent_started_t=3.0,
        agent_finished_t=4.0,
        verifier_started_t=4.0,
        verifier_finished_t=5.0,
        valid=True,
        reason=None,
        instrument=None,
        labels=[],
        measured=True,
        unmeasured_reason=None,
        oom_source=None,
        requests=1,
        error_requests=0,
        refused_requests=0,
        prompt_tokens=10,
        completion_tokens=5,
        cached_read_tokens=0,
        prompt_chars=20,
        completion_chars=10,
        cost_usd_estimate=0.01,
        harbor_reported_tokens=15,
        turns=2,
        tool_calls=1,
        median_first_content_s=0.2,
        output_rate_tokens_per_s=5.0,
        agent_wall_s=1.0,
        peak_memory_mb=None,
        memory_limit_mb=None,
    )


def test_trial_row_round_trips():
    row = _trial_row()

    assert TrialRow.from_row(row.to_row()) == row


@pytest.mark.parametrize("field", list(_trial_row().to_row()))
def test_trial_row_parser_rejects_missing_field(field):
    row = _trial_row().to_row()
    row.pop(field)

    with pytest.raises(ValueError):
        TrialRow.from_row(row)


def test_trial_row_parser_rejects_unknown_field():
    row = _trial_row().to_row()
    row["extra"] = True

    with pytest.raises(ValueError):
        TrialRow.from_row(row)
