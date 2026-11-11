from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from harbor_research_runner.trial_validity.verdict import TrialFacts

PHASES = ("environment_setup", "agent_setup", "agent", "verifier")


def failed_phase(facts: "TrialFacts") -> str | None:
    """The last phase that started, when the trial carries an exception."""
    if facts.exception_type is None:
        return None
    for phase in reversed(PHASES):
        if facts.phase_started.get(phase) is not None:
            return phase
    return None
