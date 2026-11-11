import json
from dataclasses import dataclass, fields
from typing import Any, cast


class _Event:
    def to_row(self) -> dict:
        return {
            ("pass" if item.name == "pass_number" else item.name): getattr(self, item.name)
            for item in fields(cast(Any, self))
        }

    def to_json(self) -> str:
        return json.dumps(self.to_row(), sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class Launch(_Event):
    t: float
    tool_version: str
    tool_commit: str
    pid: int
    type: str = "launch"


@dataclass(frozen=True)
class LaunchCheck(_Event):
    t: float
    name: str
    ok: bool
    detail: str
    type: str = "launch_check"


@dataclass(frozen=True)
class HarborStart(_Event):
    t: float
    pass_number: int
    command: str
    job: str
    type: str = "harbor_start"


@dataclass(frozen=True)
class HarborExit(_Event):
    t: float
    pass_number: int
    returncode: int
    job: str
    type: str = "harbor_exit"


@dataclass(frozen=True)
class TrialVerdict(_Event):
    t: float
    trial: str
    task: str
    arm: str
    valid: bool
    reason: str | None
    instrument: str | None
    labels: list[str]
    measured: bool
    unmeasured_reason: str | None
    type: str = "trial_verdict"


@dataclass(frozen=True)
class SetAside(_Event):
    t: float
    trial: str
    task: str
    arm: str
    reason: str
    type: str = "set_aside"


@dataclass(frozen=True)
class Halt(_Event):
    t: float
    reason: str
    detail: str
    type: str = "halt"


@dataclass(frozen=True)
class Resume(_Event):
    t: float
    tool_version: str
    tool_commit: str
    pid: int
    type: str = "resume"


@dataclass(frozen=True)
class Finish(_Event):
    t: float
    exit_code: int
    slots_complete: int
    slots_incomplete: int
    type: str = "finish"


_EVENT_TYPES = {
    event.type: event
    for event in (
        Launch,
        LaunchCheck,
        HarborStart,
        HarborExit,
        TrialVerdict,
        SetAside,
        Halt,
        Resume,
        Finish,
    )
}


def parse_event(row: dict):
    try:
        event_type = _EVENT_TYPES[row["type"]]
    except (KeyError, TypeError) as exc:
        raise ValueError("unknown run event type") from exc
    names = {item.name for item in fields(event_type)}
    if "pass_number" in names:
        names.remove("pass_number")
        names.add("pass")
    if set(row) != names:
        raise ValueError("run event fields do not match")
    values = dict(row)
    if "pass" in values:
        values["pass_number"] = values.pop("pass")
    try:
        return event_type(**values)
    except TypeError as exc:
        raise ValueError("invalid run event") from exc
