from harbor_research_runner.records.machine_check import MachineCheck


def test_excluded_tasks_are_sorted_tasks_missing_from_included():
    check = MachineCheck(
        created_t=1.0,
        tool_version="1",
        harbor_version="0.23.0",
        host_fingerprint="host",
        task_source={},
        concurrency=1,
        tasks=[
            {"name": name, "digest": "d"}
            for name in ["task/e", "task/a", "task/d", "task/b", "task/c"]
        ],
        included=["task/c", "task/a", "task/e"],
        verifier_timeout_multiplier=1.0,
    )

    assert check.excluded_tasks() == ["task/b", "task/d"]
