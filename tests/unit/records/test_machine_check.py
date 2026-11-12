import json

import pytest

from harbor_research_runner.records.machine_check import MachineCheck, read, write


def _machine_check():
    return MachineCheck(
        created_t=1.0,
        tool_version="v",
        harbor_version="h",
        host_fingerprint="fingerprint",
        task_source={},
        concurrency=1,
        tasks=[
            {
                "name": "bn-fit-modify",
                "agent_budget_s": 3600.0,
                "verifier_budget_s": 60.0,
            }
        ],
        included=[],
        verifier_timeout_multiplier=2.0,
    )


def test_machine_check_round_trips():
    check = _machine_check()

    assert MachineCheck.from_row(check.to_row()) == check


@pytest.mark.parametrize("field", list(_machine_check().to_row()))
def test_machine_check_parser_rejects_missing_field(field):
    row = _machine_check().to_row()
    row.pop(field)

    with pytest.raises(ValueError):
        MachineCheck.from_row(row)


def test_machine_check_parser_rejects_unknown_field():
    row = _machine_check().to_row()
    row["extra"] = True

    with pytest.raises(ValueError):
        MachineCheck.from_row(row)


def test_machine_check_write_overwrites_and_reads(tmp_path):
    path = tmp_path / "machine-check.json"
    first = _machine_check()
    second = MachineCheck(
        created_t=2.0,
        tool_version="v2",
        harbor_version="h",
        host_fingerprint="fingerprint",
        task_source={},
        concurrency=1,
        tasks=[],
        included=[],
        verifier_timeout_multiplier=2.0,
    )

    write(path, first)
    write(path, second)
    assert read(path) == second
    assert json.loads(path.read_text()) == second.to_row()
