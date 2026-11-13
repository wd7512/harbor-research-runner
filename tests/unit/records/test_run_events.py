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
    TrialVerdict,
    parse_event,
)

EVENTS = [
    Launch(1.0, "v", "c", 10),
    LaunchCheck(1.0, "docker", True, "ok"),
    HarborStart(1.0, 0, "harbor run", "main"),
    HarborExit(1.0, 0, 0, "main"),
    StopLaunchingAt(1.0, 0, "2030-01-01T00:00:00+00:00"),
    TrialVerdict(1.0, "tr", "task", "arm", True, None, None, [], True, None),
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
