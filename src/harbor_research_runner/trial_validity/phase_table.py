from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from harbor_research_runner.trial_validity.verdict import TrialFacts

PHASES = ("environment_setup", "agent_setup", "agent", "verifier")


def failed_phase(facts: "TrialFacts") -> str | None:
    """Return the phase active when the trial exception occurred."""
    if facts.exception_type is None:
        return None
    for phase in reversed(PHASES):
        started = facts.phase_started.get(phase)
        if started is not None and (facts.exception_at is None or started <= facts.exception_at):
            return phase
    return None
