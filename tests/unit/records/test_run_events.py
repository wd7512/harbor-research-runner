import pytest

from harbor_research_runner.records.run_events import (
    Finish,
    Halt,
    HarborExit,
    HarborStart,
    Launch,
    LaunchCheck,
    Resume,
    SetAside,
    StopLaunchingAt,
    TelemetryStopped,
    TrialMeasurementAmended,
    TrialVerdict,
    final_verdicts,
    parse_event,
    telemetry_finished,
)

EVENTS = [
    Launch(1.0, "v", "c", 10),
    LaunchCheck(1.0, "docker", True, "ok"),
    HarborStart(1.0, 0, "harbor run", "main"),
    HarborExit(1.0, 0, 0, "main"),
    StopLaunchingAt(1.0, 0, "2030-01-01T00:00:00+00:00"),
    TrialVerdict(1.0, "tr", "task", "arm", True, None, None, [], True, None, None),
    TrialMeasurementAmended(2.0, "tr", "telemetry_damaged"),
    TelemetryStopped(2.0),
    SetAside(1.0, "tr", "task", "arm", "bad"),
    Halt(1.0, "inactive", "detail"),
    Resume(1.0, "v", "c", 10),
    Finish(1.0, 0, 1, 0),
]


@pytest.mark.parametrize("event", EVENTS, ids=[event.type for event in EVENTS])
def test_run_event_round_trips(event):
    assert parse_event(event.to_row()) == event


@pytest.mark.parametrize(
    ("event", "field"),
    [(event, field) for event in EVENTS for field in event.to_row()],
    ids=[f"{event.type}-{field}" for event in EVENTS for field in event.to_row()],
)
def test_run_event_parser_rejects_missing_field(event, field):
    row = event.to_row()
    row.pop(field)

    with pytest.raises(ValueError):
        parse_event(row)


@pytest.mark.parametrize("event", EVENTS, ids=[event.type for event in EVENTS])
def test_run_event_parser_rejects_unknown_field(event):
    row = event.to_row()
    row["extra"] = True

    with pytest.raises(ValueError):
        parse_event(row)


def test_final_verdicts_applies_amendment():
    verdict = TrialVerdict(1.0, "tr", "task", "arm", True, None, None, [], True, None)

    assert (
        final_verdicts(
            [verdict.to_row(), TrialMeasurementAmended(2.0, "tr", "telemetry_damaged").to_row()]
        )["tr"]["unmeasured_reason"]
        == "telemetry_damaged"
    )
    assert (
        final_verdicts(
            [verdict.to_row(), TrialMeasurementAmended(2.0, "tr", "telemetry_damaged").to_row()]
        )["tr"]["measured"]
        is False
    )


def test_final_verdicts_ignores_amendment_for_already_unmeasured_trial():
    verdict = TrialVerdict(
        1.0, "tr", "task", "arm", True, None, None, [], False, "usage_incomplete"
    )

    assert (
        final_verdicts(
            [verdict.to_row(), TrialMeasurementAmended(2.0, "tr", "telemetry_damaged").to_row()]
        )["tr"]["unmeasured_reason"]
        == "usage_incomplete"
    )


def test_final_verdicts_keeps_first_amendment_reason():
    verdict = TrialVerdict(1.0, "tr", "task", "arm", True, None, None, [], True, None)

    result = final_verdicts(
        [
            verdict.to_row(),
            TrialMeasurementAmended(2.0, "tr", "telemetry_damaged").to_row(),
            TrialMeasurementAmended(3.0, "tr", "host_samples_damaged").to_row(),
        ]
    )

    assert result["tr"]["unmeasured_reason"] == "telemetry_damaged"


def test_final_verdicts_leaves_unamended_trial_unchanged():
    verdict = TrialVerdict(1.0, "tr", "task", "arm", True, None, None, [], True, None)

    assert final_verdicts([verdict.to_row()])["tr"] == verdict.to_row()


def test_telemetry_finished_when_stop_follows_last_harbor_start():
    events = [HarborStart(1.0, 0, "harbor run", "main").to_row(), TelemetryStopped(2.0).to_row()]

    assert telemetry_finished(events) is True


def test_telemetry_not_finished_without_stop_marker():
    events = [HarborStart(1.0, 0, "harbor run", "main").to_row()]

    assert telemetry_finished(events) is False


def test_telemetry_not_finished_when_harbor_restarts_after_stop():
    events = [
        HarborStart(1.0, 0, "harbor run", "main").to_row(),
        TelemetryStopped(2.0).to_row(),
        HarborStart(3.0, 1, "harbor run", "main").to_row(),
    ]

    assert telemetry_finished(events) is False
