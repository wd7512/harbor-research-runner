import os
from pathlib import Path

import pytest

from harbor_research_runner.records.limits import Limits
from harbor_research_runner.statistics.rates import arm_rates
from harbor_research_runner.trial_validity.upstream import upstream_reason
from tests.support.old_evidence import (
    old_null_end_count,
    old_request_rows,
    old_trial_rows,
)

pytestmark = [
    pytest.mark.replay,
    pytest.mark.skipif(not os.environ.get("HRR_OLD_REPO"), reason="HRR_OLD_REPO is unset"),
]

EVIDENCE = Path("evidence/ollama-beam-119-vs-21-k3-i5-20261106T000709307600Z")
UPSTREAM_CHUNKS = (
    (0, {"compile-compcert__UpgyXXG": "upstream_terminal_error"}, 1, 0),
    (100, {}, 0, 0),
    (200, {}, 2, 2),
    (300, {}, 0, 0),
    (400, {"multi-source-data-merger__cMSorkz": "upstream_terminal_error"}, 2, 1),
    (500, {}, 0, 0),
    (600, {}, 2, 2),
    (700, {}, 0, 0),
)


@pytest.fixture(scope="module")
def evidence():
    evidence_dir = Path(os.environ["HRR_OLD_REPO"]) / EVIDENCE
    return (
        old_trial_rows(evidence_dir),
        old_request_rows(evidence_dir),
        old_null_end_count(evidence_dir),
    )


def test_beam_evidence_pass_counts_by_reward(evidence):
    trial_rows, _, _ = evidence
    rates = arm_rates(trial_rows, "final")

    assert (rates["1.19.3"].passes, rates["1.19.3"].trials) == (189, 267)
    assert (rates["2.1"].passes, rates["2.1"].trials) == (189, 267)


@pytest.mark.parametrize(
    ("start", "expected_invalid", "expected_single_error", "expected_valid_single_error"),
    UPSTREAM_CHUNKS,
    ids=[f"trials-{start}-{min(start + 99, 745)}" for start, *_ in UPSTREAM_CHUNKS],
)
def test_beam_evidence_upstream_verdicts(
    evidence, start, expected_invalid, expected_single_error, expected_valid_single_error
):
    _, request_rows, null_end_count = evidence
    limits = Limits.from_dict({}, repetitions=1, verifier_timeout_multiplier=1.0)
    trials = sorted(request_rows)
    chunk = trials[start : start + 100]
    verdicts = {trial: upstream_reason(request_rows[trial], limits) for trial in chunk}
    invalid = {trial: reason for trial, reason in verdicts.items() if reason is not None}
    single_error = {
        trial
        for trial in chunk
        if sum(row["upstream_status"] != "ok" for row in request_rows[trial]) == 1
    }

    assert len(trials) == 746
    assert sum(chunk[2] for chunk in UPSTREAM_CHUNKS) == 7
    assert sum(chunk[3] for chunk in UPSTREAM_CHUNKS) == 5
    assert invalid == expected_invalid
    assert len(single_error) == expected_single_error
    assert sum(verdicts[trial] is None for trial in single_error) == expected_valid_single_error


def test_beam_evidence_null_end_duration_substitution_is_visible(evidence):
    _, request_rows, null_end_count = evidence

    assert null_end_count == 48
    assert sum(row["duration_s"] == 0.0 for rows in request_rows.values() for row in rows) == 48


def test_beam_evidence_tokens_outside_final_set(evidence):
    trial_rows, request_rows, _ = evidence
    final_trials = {row.trial for row in trial_rows}
    outside = set(request_rows) - final_trials

    assert len(outside) == 212
    assert sum(len(request_rows[trial]) for trial in outside) == 12_810
    assert sum(map(len, request_rows.values())) == 33_615
