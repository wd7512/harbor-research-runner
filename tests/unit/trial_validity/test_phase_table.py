import pytest

from harbor_research_runner.trial_validity.phase_table import PHASES, failed_phase
from harbor_research_runner.trial_validity.verdict import TrialFacts


def _facts(exception_type=None, **started):
    return TrialFacts(
        trial="trial",
        task="task",
        arm="arm",
        has_result=True,
        reward=0.0,
        exception_type=exception_type,
        phase_started={phase: started.get(phase) for phase in PHASES},
        phase_finished={phase: None for phase in PHASES},
        harbor_reported_tokens=0,
    )


def test_phases_are_in_lifecycle_order():
    assert PHASES == ("environment_setup", "agent_setup", "agent", "verifier")


def test_no_exception_returns_none():
    assert failed_phase(_facts(**{"agent": 1.0})) is None


def test_last_started_phase_is_returned():
    facts = _facts("Error", environment_setup=1.0, agent_setup=2.0, agent=3.0, verifier=4.0)

    assert failed_phase(facts) == "verifier"


def test_unstarted_later_phase_is_ignored():
    facts = _facts("Error", environment_setup=1.0, agent_setup=2.0, agent=3.0)

    assert failed_phase(facts) == "agent"


@pytest.mark.parametrize("phase", PHASES)
def test_each_started_phase_is_attributed(phase):
    facts = _facts("Error", **{phase: 1.0})

    assert failed_phase(facts) == phase


def test_no_started_phase_returns_none():
    assert failed_phase(_facts("Error")) is None
